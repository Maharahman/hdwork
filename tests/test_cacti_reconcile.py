import contextlib
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'integrations/cacti-agent'))
import reconcile as agent
import reconcile_forms as forms
from test_cacti_edit import form
from test_cacti_delete import confirmation


def source():
    row = {'sid': '1165651712', 'source_row': 114, 'status': 'OK', 'server': 'SBS', 'customer_name': 'BANK JATIM', 'customer_detail': 'KF KENJERAN',
           'olt_ip': '172.23.225.42', 'slot': '8', 'port': '6', 'onu': '19', 'serial_number': 'ZTEGCC82D5F7'}
    config = agent.desired(row)
    mapping = {'sid': row['sid'], 'server': 'SBS', 'tree_id': 34, 'host_id': 8471, 'header_id': 9127, 'ownership_status': 'VERIFIED', 'verified_at': 'now',
               'sync_state': 'ACTIVE', 'last_applied_json': config}
    return {'spreadsheet_id': 'sheet', 'snapshot_at': 'now', 'customers': [row], 'aliases': [], 'conflicts': [],
            'inventory': {'version': 1, 'complete': True, 'safe_for_delete': True, 'rows': [copy.deepcopy(row)]}, 'mappings': [mapping],
            'headers': [{'server': 'SBS', 'tree_id': 34, 'normalized_name': 'BANK JATIM', 'header_id': 9127}], 'masters': [{'sid': row['sid'], 'source_status': 'OK'}]}


def inventory():
    return [{'server': 'SBS', 'host_id': 8471, 'description': agent.desired(source()['customers'][0])['description'],
             'memberships': [{'tree_id': 34, 'header_id': 9127, 'header': 'BANK JATIM'}]}]


def client():
    c = Mock(server='SBS', tree=34, base='http://cacti/ont')
    c.headers.return_value = {'BANK JATIM': [{'header_id': 9127}]}
    return c


class ReconcileTests(unittest.TestCase):
    def test_plan_cached_no_change_and_no_writes(self):
        c = client()
        with patch.object(agent, 'read_host') as read:
            rows = agent.plan_rows(source(), inventory(), {'SBS': c})
        self.assertEqual(rows[0]['operation'], 'NO_CHANGE')
        read.assert_not_called(); c.session.post.assert_not_called()

    def test_create_update_delete_cleanup_missing_conflict_and_protection(self):
        c = client()
        for mode, expected in [('CREATE', 'CREATE'), ('UPDATE', 'UPDATE'), ('NOT', 'DELETE'), ('CLEANUP', 'CLEANUP'), ('MISSING', 'HOLD'), ('CONFLICT', 'HOLD'), ('ATCS', 'HOLD'), ('MISSING-HOST', 'HOLD')]:
            s, inv = source(), inventory()
            if mode == 'CREATE': s['mappings'] = []; inv = []
            if mode == 'UPDATE': s['customers'][0]['slot'] = '9'
            if mode in ('NOT', 'CLEANUP'): s['inventory']['rows'][0]['status'] = 'NOT'; s['customers'] = []
            if mode == 'CLEANUP': s['mappings'][0]['sync_state'] = 'DELETED'; inv = []
            if mode == 'MISSING': s['inventory']['rows'] = []; s['customers'] = []
            if mode == 'CONFLICT': s['inventory']['rows'].append(copy.deepcopy(s['inventory']['rows'][0]))
            if mode == 'ATCS': s['inventory']['rows'][0]['customer_name'] = 'ATCS'
            if mode == 'MISSING-HOST': inv = []
            with patch.object(agent, 'read_host', return_value={'config': agent.desired(source()['customers'][0])}):
                rows = agent.plan_rows(s, inv, {'SBS': c})
            self.assertEqual(rows[0]['operation'], expected, mode)

    def test_server_header_move_and_unmapped_prefix_candidate_never_adopted(self):
        for mode in ('server', 'header', 'candidate', 'bad-owner'):
            s, inv = source(), inventory()
            if mode == 'server': s['mappings'][0]['server'] = 'SBU'
            if mode == 'header': s['mappings'][0]['header_id'] = 2
            if mode == 'candidate': s['mappings'] = []
            if mode == 'bad-owner': s['mappings'][0]['ownership_status'] = 'REVIEW_REQUIRED'
            self.assertEqual(agent.plan_rows(s, inv, {'SBS': client()})[0]['operation'], 'HOLD')

    def test_zero_is_value_and_large_delete_set_is_held(self):
        s = source(); s['customers'][0].update(slot='0', port='0', onu='0')
        self.assertEqual(agent.desired(s['customers'][0])['onu'], '0')
        s['inventory']['rows'][0]['status'] = 'NOT'; s['customers'] = []
        for i in range(11):
            sid = str(i)
            s['inventory']['rows'].append({**s['inventory']['rows'][0], 'sid': sid})
            s['mappings'].append({**s['mappings'][0], 'sid': sid, 'host_id': 9000 + i})
        self.assertTrue(all(r['operation'] == 'HOLD' for r in agent.plan_rows(s, inventory(), {'SBS': client()}, delete_limit=10)))

    def test_source_and_mapping_rechecked_before_remote_write(self):
        s = source(); m = s['mappings'][0]
        job = {'request': {'sid': m['sid'], 'operation': 'UPDATE'}, 'expected': {'mapping': agent.identity(m), 'config': agent.desired(s['customers'][0]), 'server': 'SBS', 'institution': 'BANK JATIM'}}
        agent.check_current(s, job)
        for change in ('slot', 'mapping', 'NOT', 'duplicate'):
            x = copy.deepcopy(s)
            if change == 'slot': x['customers'][0]['slot'] = '9'
            if change == 'mapping': x['mappings'][0]['host_id'] = 3199
            if change == 'NOT': x['inventory']['rows'][0]['status'] = 'NOT'
            if change == 'duplicate': x['inventory']['rows'].append(copy.deepcopy(x['inventory']['rows'][0]))
            with self.assertRaises(RuntimeError): agent.check_current(x, job)

    def test_generic_edit_preserves_secret_options_and_only_changes_allowed_values(self):
        c = client(); c.session.get.return_value = Mock(text=form(), url=c.base + '/host.php')
        with patch.object(forms, 'checked_html', side_effect=lambda r: __import__('bs4').BeautifulSoup(r.text, 'html.parser')):
            live = forms.read_host(c, '1165651712', 8471)
        config = {**live['config'], 'slot': '0', 'port': '0', 'onu': '0'}
        payload = forms.edit_payload(live, config)
        self.assertEqual({k for k in payload if payload[k] != live['payload'][k]}, {'slot', 'port', 'onu_id'})
        self.assertEqual(payload['snmp_password'], 'SECRET-PASSWORD')
        self.assertNotIn('disabled', payload)
        self.assertEqual(forms.option_guard(payload), live['option_guard'])

    def test_delete_confirmation_only_exact_owned_host_and_preserve_graphs(self):
        response = Mock(text=str(confirmation()), url='http://cacti/ont/host.php')
        _, payload = forms.delete_confirmation(response, 8471, source()['mappings'][0]['last_applied_json']['description'])
        self.assertEqual(payload['delete_type'], '1')
        with self.assertRaises(RuntimeError): forms.delete_confirmation(response, 3199, 'EXTERNAL')
        with self.assertRaises(RuntimeError): forms.require_exclusive([{'tree_id': 34, 'header_id': 9127}, {'tree_id': 14, 'header_id': 99}], 34, 9127)

    def test_lost_complete_ack_replays_receipt_without_any_remote_write(self):
        with tempfile.TemporaryDirectory() as d:
            api, c = Mock(), client()
            state = {'run_id': 'run', 'token': 'secret', 'spreadsheet_id': 'sheet', 'jobs': []}
            worker = agent.Reconciler(api, {'SBS': c}, state, Path(d) / 'journal')
            job = {'id': 'job', 'request': {'sid': '1165651712', 'operation': 'UPDATE'}, 'local_status': 'ATTEMPTED', 'receipt': {'verified': True}}
            api.request.return_value = {'state': 'ATTEMPTED', 'expected': {}}
            worker.process(job)
            self.assertEqual(job['local_status'], 'VERIFIED')
            self.assertEqual(api.request.call_args.args[1], '/api/datin-cacti/sync/jobs/complete')
            c.session.post.assert_not_called()

    def test_timeout_update_journal_prevents_second_post_and_allows_readback_recovery(self):
        with tempfile.TemporaryDirectory() as d:
            s, c, api = source(), client(), Mock()
            state = {'run_id': 'run', 'token': 'secret', 'spreadsheet_id': 'sheet', 'jobs': []}
            worker = agent.Reconciler(api, {'SBS': c}, state, Path(d) / 'journal')
            config = agent.desired(s['customers'][0]); before = {'config': config, 'option_guard': 'guard', 'url': c.base+'/host.php'}
            after = {**config, 'slot': '9'}; s['customers'][0]['slot'] = '9'
            job = {'request': {'sid': '1165651712', 'operation': 'UPDATE', 'observed': {'config': config, 'option_guard': 'guard'}},
                   'expected': {'operation': 'UPDATE', 'mapping': agent.identity(s['mappings'][0]), 'config': after, 'server': 'SBS', 'tree_id': 34, 'header_id': 9127, 'institution': 'BANK JATIM'}}
            state['jobs'] = [job]
            c.session.post.side_effect = TimeoutError()
            with patch.object(worker, 'source', return_value=s), patch.object(agent, 'read_host', return_value=before), patch.object(agent, 'device_actions', return_value=('url', {}, {34})), patch.object(agent, 'references', return_value=[{'tree_id': 34, 'header_id': 9127}]), patch.object(agent, 'edit_payload', return_value={}), patch.object(agent.time, 'sleep'):
                with self.assertRaises(TimeoutError): worker.update(job)
                self.assertTrue(json.loads((Path(d)/'journal').read_text())['jobs'][0]['update_attempted'])
                with self.assertRaises(RuntimeError): worker.update(job)
                with patch.object(agent, 'read_host', return_value={'config': after, 'option_guard': 'guard'}), patch.object(worker, 'host_receipt', return_value={'ok': True}):
                    self.assertEqual(worker.update(job), {'ok': True})
            self.assertEqual(c.session.post.call_count, 1)

    def test_already_absent_NOT_deletion_only_reconciles_database_and_no_cacti_post(self):
        with tempfile.TemporaryDirectory() as d:
            s, c = source(), client(); s['customers']=[]; s['inventory']['rows'][0]['status']='NOT'
            worker=agent.Reconciler(Mock(), {'SBS':c}, {'jobs':[]}, Path(d)/'journal')
            job={'request':{'operation':'DELETE','sid':'1165651712'},'expected':{'mapping':agent.identity(s['mappings'][0])}}
            with patch.object(agent,'fetch_inventory',return_value=[{'host_id':3199}]), patch.object(agent,'references',return_value=[]):
                receipt=worker.delete(job)
            self.assertTrue(receipt['absent']); self.assertTrue(receipt['other_host_ids_unchanged'])
            c.session.post.assert_not_called()

    def test_uncertain_delete_never_repeats_final_confirmation(self):
        with tempfile.TemporaryDirectory() as d:
            c=client(); worker=agent.Reconciler(Mock(),{'SBS':c},{'jobs':[]},Path(d)/'journal')
            job={'request':{'operation':'DELETE','sid':'1165651712'},'expected':{'mapping':agent.identity(source()['mappings'][0])},'delete_attempted':True,'before_host_ids':[8471,3199], 'before_headers':c.headers()}
            with patch.object(agent,'fetch_inventory',return_value=[{'host_id':8471},{'host_id':3199}]), self.assertRaisesRegex(RuntimeError,'tidak ulang'):
                worker.delete(job)
            c.session.post.assert_not_called()

    def test_fresh_create_attaches_once_and_resume_never_creates_again(self):
        from test_cacti_pilot import form as create_form
        with tempfile.TemporaryDirectory() as d:
            s, c = source(), client(); s['mappings']=[]
            state={'jobs':[], 'spreadsheet_id':'sheet'}
            worker=agent.Reconciler(Mock(), {'SBS':c}, state, Path(d)/'journal')
            config=agent.desired(s['customers'][0])
            job={'request':{'operation':'CREATE','sid':'1165651712'}, 'expected':{'operation':'CREATE','mapping':None,'config':config,'server':'SBS','tree_id':34,'header_id':9127,'institution':'BANK JATIM'}}
            state['jobs']=[job]
            attach='<form action="tree.php"><input name="graph_tree_id" value="34"><input name="type" value="3"><input name="id" value="0"><input name="action" value="save"><select name="parent_item_id"><option value="9127" selected>BANK JATIM</option></select><select name="host_id"><option value="110">new</option></select></form>'
            c.session.get.side_effect=[Mock(text=create_form(),url=c.base+'/host.php'),Mock(text='tree'),Mock(text=attach,url=c.base+'/tree.php')]
            newhost={'host_id':110,'server':'SBS','description':config['description']}
            with patch.object(worker,'source',return_value=s), patch.object(worker,'inventory',side_effect=[[],[newhost]]), patch.object(agent,'fetch_inventory',return_value=[{'host_id':3199}]), patch.object(agent,'read_host',return_value={'config':config}), patch.object(worker,'host_receipt',return_value={'host_id':110}), patch.object(agent,'parse_tree_memberships',return_value={}), patch.object(agent.time,'sleep'):
                self.assertEqual(worker.create(job),{'host_id':110})
            self.assertTrue(job['create_attempted']);self.assertTrue(job['attach_attempted'])
            self.assertEqual(c.session.post.call_count,2)
            c.session.get.side_effect=None;c.session.get.return_value=Mock(text='tree')
            with patch.object(worker,'inventory',return_value=[newhost]), patch.object(agent,'read_host',return_value={'config':config}), patch.object(worker,'host_receipt',return_value={'host_id':110}), patch.object(agent,'parse_tree_memberships',return_value={110:[{'tree_id':34,'header_id':9127}]}):
                worker.create(job)
            self.assertEqual(c.session.post.call_count,2)

    def test_delete_timeout_reconciles_absence_without_repeating_confirmation(self):
        with tempfile.TemporaryDirectory() as d:
            s,c=source(),client();s['customers']=[];s['inventory']['rows'][0]['status']='NOT'
            config=s['mappings'][0]['last_applied_json'];mapping=agent.identity(s['mappings'][0])
            state={'jobs':[]}; worker=agent.Reconciler(Mock(),{'SBS':c},state,Path(d)/'journal')
            job={'request':{'operation':'DELETE','sid':'1165651712','observed':{'config':config,'option_guard':'guard'}},'expected':{'operation':'DELETE','mapping':mapping,'row':s['inventory']['rows'][0]}}
            state['jobs']=[job]
            c.session.post.side_effect=[Mock(text=str(confirmation()),url=c.base+'/host.php'),TimeoutError()]
            with patch.object(worker,'source',return_value=s), patch.object(agent,'fetch_inventory',return_value=[{'host_id':8471},{'host_id':3199}]), patch.object(agent,'read_host',return_value={'config':config,'option_guard':'guard'}), patch.object(agent,'device_actions',return_value=(c.base+'/host.php',{'action':'actions'},{34})), patch.object(agent,'references',return_value=[{'tree_id':34,'header_id':9127}]),patch.object(agent.time,'sleep'):
                with self.assertRaises(TimeoutError):worker.delete(job)
            self.assertTrue(job['delete_attempted']);self.assertEqual(c.session.post.call_count,2)
            with patch.object(agent,'fetch_inventory',return_value=[{'host_id':3199}]),patch.object(agent,'references',return_value=[]):
                receipt=worker.delete(job)
            self.assertTrue(receipt['absent']);self.assertEqual(c.session.post.call_count,2)

    def test_changed_source_after_remote_write_still_records_old_result_then_replans(self):
        with tempfile.TemporaryDirectory() as d:
            state={'jobs':[],'run_id':'run','token':'secret','spreadsheet_id':'sheet'}
            worker=agent.Reconciler(Mock(),{'SBS':client()},state,Path(d)/'journal')
            job={'id':'job','request':{'operation':'UPDATE','sid':'1165651712'},'local_status':'ATTEMPTED','update_attempted':True}
            with patch.object(worker,'call',side_effect=[{'state':'ATTEMPTED','expected':{'operation':'UPDATE'}},{'state':'VERIFIED'}]),patch.object(worker,'source',side_effect=RuntimeError('source changed')),patch.object(worker,'update',return_value={'old_result':True}) as update,patch('builtins.print'):
                worker.process(job)
            update.assert_called_once();self.assertEqual(job['local_status'],'VERIFIED')

    def test_authorized_but_never_written_job_can_cancel_when_source_changes(self):
        with tempfile.TemporaryDirectory() as d:
            state={'jobs':[],'run_id':'run','token':'secret','spreadsheet_id':'sheet'}
            worker=agent.Reconciler(Mock(),{'SBS':client()},state,Path(d)/'journal')
            job={'id':'job','request':{'operation':'UPDATE','sid':'1165651712'},'local_status':'ATTEMPTED'}
            with patch.object(worker,'call',side_effect=[{'state':'ATTEMPTED','expected':{}},{'success':True}]) as call,patch.object(worker,'source',side_effect=RuntimeError('source changed')):
                worker.process(job)
            self.assertEqual(job['local_status'],'CANCELLED');self.assertTrue(call.call_args.args[1]['no_cacti_write'])

    def test_watch_never_overlaps_and_waits_ten_minutes_after_a_cycle(self):
        with patch.object(sys,'argv',['agent','--watch','--apply']),patch.object(agent,'load_dotenv'),patch.object(agent,'single_instance',return_value=contextlib.nullcontext()),patch.object(agent,'cycle') as cycle,patch.object(agent.time,'sleep',side_effect=KeyboardInterrupt()) as sleep,contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(agent.main(),0)
        cycle.assert_called_once();sleep.assert_called_once_with(600)

    def test_backend_offline_fails_before_cacti_and_watch_requires_explicit_apply(self):
        api=Mock();api.request.side_effect=agent.AgentError(404,'not deployed')
        args=Mock(apply=True)
        with patch.object(agent,'AgentAPI',return_value=api), patch.object(agent,'Cacti') as c, self.assertRaises(RuntimeError):
            agent.cycle(args)
        c.assert_not_called()
        for argv in (['agent','--watch'],['agent','--apply','--plan'],['agent','--watch','--apply','--interval','60']):
            with patch.object(sys,'argv',argv),contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit): agent.main()


if __name__ == '__main__':
    unittest.main()
