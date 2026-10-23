import { useEffect, useState } from "react"
import { KeyRound, Plus, Search, UserCog } from "lucide-react"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { useAuth } from "@/contexts/AuthContext"
import {
  createManagedUser,
  getManagedUsers,
  getUserOptions,
  resetManagedPassword,
  updateManagedUser,
  type ManagedUser,
  type UserOptions,
} from "@/services/user.service"

const emptyForm = {
  name: "",
  username: "",
  password: "",
  role: "HD",
  city_id: 0,
  is_active: true,
  segment_ids: [] as number[],
}

export default function UserManagementPage() {
  const { token, user } = useAuth()
  const isSuper = user?.role === "SUPER_ADMIN"

  const [users, setUsers] = useState<ManagedUser[]>([])
  const [options, setOptions] = useState<UserOptions>({
    cities: [],
    segments: [],
    roles: [],
  })
  const [cityId, setCityId] = useState("")
  const [search, setSearch] = useState("")
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState("")

  const [dialogOpen, setDialogOpen] = useState(false)
  const [editing, setEditing] = useState<ManagedUser | null>(null)
  const [form, setForm] = useState(emptyForm)
  const [saving, setSaving] = useState(false)

  // Local-only state for UI checkboxes (no DB binding)
  const [coverageServices, setCoverageServices] = useState<string[]>([])

  const [resetUser, setResetUser] = useState<ManagedUser | null>(null)
  const [newPassword, setNewPassword] = useState("")

  const load = async () => {
    if (!token) return
    try {
      setLoading(true)
      setError("")
      setUsers(await getManagedUsers(token, { cityId, search }))
    } catch (e) {
      setError(e instanceof Error ? e.message : "Gagal mengambil user")
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    if (!token) return
    getUserOptions(token)
      .then(setOptions)
      .catch((e) =>
        setError(e instanceof Error ? e.message : "Gagal mengambil opsi")
      )
  }, [token])

  useEffect(() => {
    const timer = setTimeout(load, 180)
    return () => clearTimeout(timer)
  }, [token, cityId, search])

  if (user?.role !== "ADMIN" && user?.role !== "SUPER_ADMIN") {
    return (
      <div className="p-6 text-muted-foreground">
        Halaman ini hanya untuk admin.
      </div>
    )
  }

  const openCreate = () => {
    setEditing(null)
    setForm({
      ...emptyForm,
      city_id: options.cities[0]?.id || 0,
      role: options.roles[0] || "HD",
    })
    setCoverageServices([]) // Reset UI checkboxes
    setDialogOpen(true)
  }

  const openEdit = (managedUser: ManagedUser) => {
    setEditing(managedUser)
    setForm({
      name: managedUser.name,
      username: managedUser.username,
      password: "",
      role: managedUser.role,
      city_id: managedUser.city_id,
      is_active: managedUser.is_active,
      segment_ids: managedUser.segment_ids,
    })
    setCoverageServices([]) // Reset UI checkboxes
    setDialogOpen(true)
  }

  const save = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!token) return
    try {
      setSaving(true)
      setError("")
      // DB payload remains original (no coverage services sent to DB)
      const payload = {
        ...form,
        password: form.password || undefined,
      }
      if (editing) {
        await updateManagedUser(token, editing.id, payload)
      } else {
        await createManagedUser(token, payload)
      }
      setDialogOpen(false)
      await load()
    } catch (err) {
      setError(err instanceof Error ? err.message : "Gagal menyimpan user")
    } finally {
      setSaving(false)
    }
  }

  const reset = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!token || !resetUser) return
    try {
      setSaving(true)
      await resetManagedPassword(token, resetUser.id, newPassword)
      setResetUser(null)
      setNewPassword("")
    } catch (err) {
      setError(err instanceof Error ? err.message : "Gagal reset password")
    } finally {
      setSaving(false)
    }
  }

  const toggleSegment = (id: number) => {
    setForm((current) => ({
      ...current,
      segment_ids: current.segment_ids.includes(id)
        ? current.segment_ids.filter((value) => value !== id)
        : [...current.segment_ids, id],
    }))
  }

  const toggleCoverageService = (service: string) => {
    setCoverageServices((prev) =>
      prev.includes(service)
        ? prev.filter((item) => item !== service)
        : [...prev, service]
    )
  }

  return (
    <div className="min-h-screen bg-muted p-6">
      <div className="mx-auto max-w-7xl space-y-6">
        {/* Header Bar */}
        <div className="sticky top-0 z-20 flex flex-wrap items-center justify-between gap-3 border-b bg-muted/95 py-3 backdrop-blur">
          <div>
            <h1 className="text-3xl font-semibold">User Management</h1>
            <p className="text-muted-foreground">Kelola akses dan coverage user</p>
          </div>
          <Button onClick={openCreate}>
            <Plus />
            Tambah User
          </Button>
        </div>

        {error && (
          <p className="rounded-lg bg-destructive/10 px-3 py-2 text-sm text-destructive">
            {error}
          </p>
        )}

        {/* Filter Card */}
        <Card>
          <CardContent className="flex flex-wrap items-center gap-3 pt-5">
            <div className="relative">
              <Search className="absolute left-3 top-2.5 size-4 text-muted-foreground" />
              <input
                className="h-9 w-64 rounded-lg border bg-background pl-9 pr-3 text-sm"
                placeholder="Cari nama atau username"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
              />
            </div>

            {isSuper && (
              <select
                className="h-9 rounded-lg border bg-background px-3 text-sm"
                value={cityId}
                onChange={(e) => setCityId(e.target.value)}
              >
                <option value="">Semua kota</option>
                {options.cities.map((city) => (
                  <option key={city.id} value={city.id}>
                    {city.name}
                  </option>
                ))}
              </select>
            )}

            <span className="text-sm text-muted-foreground">
              {users.length} user
            </span>
          </CardContent>
        </Card>

        {/* User Table Card */}
        <Card>
          <CardHeader>
            <CardTitle>Daftar User</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b text-left text-muted-foreground">
                    <th className="p-3">User</th>
                    <th className="p-3">Role</th>
                    <th className="p-3">Kota</th>
                    <th className="p-3">Coverage</th>
                    <th className="p-3">Status</th>
                    <th className="p-3">Aksi</th>
                  </tr>
                </thead>
                <tbody>
                  {users.map((managedUser) => (
                    <tr className="border-b" key={managedUser.id}>
                      <td className="p-3">
                        <div className="font-medium">{managedUser.name}</div>
                        <div className="text-muted-foreground">
                          @{managedUser.username}
                        </div>
                      </td>
                      <td className="p-3">
                        <Badge variant="secondary">{managedUser.role}</Badge>
                      </td>
                      <td className="p-3">{managedUser.city_name}</td>
                      <td className="p-3">{managedUser.segments}</td>
                      <td className="p-3">
                        <Badge
                          variant={managedUser.is_active ? "default" : "outline"}
                        >
                          {managedUser.is_active ? "Aktif" : "Nonaktif"}
                        </Badge>
                      </td>
                      <td className="p-3">
                        <div className="flex gap-2">
                          <Button
                            size="sm"
                            variant="outline"
                            onClick={() => openEdit(managedUser)}
                          >
                            <UserCog />
                            Edit
                          </Button>
                          <Button
                            size="icon-sm"
                            variant="outline"
                            title="Reset password"
                            onClick={() => setResetUser(managedUser)}
                          >
                            <KeyRound />
                          </Button>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>

              {loading ? (
                <p className="py-8 text-center text-muted-foreground">
                  Memuat user...
                </p>
              ) : users.length === 0 ? (
                <p className="py-8 text-center text-muted-foreground">
                  Belum ada user.
                </p>
              ) : null}
            </div>
          </CardContent>
        </Card>

        {/* Modal Tambah / Edit User */}
        <Dialog open={dialogOpen} onOpenChange={setDialogOpen}>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>
                {editing ? "Edit User" : "Tambah User"}
              </DialogTitle>
              <DialogDescription>
                Atur profil, akses, dan coverage user.
              </DialogDescription>
            </DialogHeader>

            <form className="space-y-4" onSubmit={save}>
              <Field label="Nama">
                <input
                  required
                  className="h-9 w-full rounded-lg border bg-background px-3 text-sm"
                  value={form.name}
                  onChange={(e) => setForm({ ...form, name: e.target.value })}
                />
              </Field>

              <Field label="Username">
                <input
                  required
                  className="h-9 w-full rounded-lg border bg-background px-3 text-sm"
                  value={form.username}
                  onChange={(e) =>
                    setForm({ ...form, username: e.target.value })
                  }
                />
              </Field>

              {!editing && (
                <Field label="Password awal">
                  <input
                    required
                    minLength={8}
                    type="password"
                    className="h-9 w-full rounded-lg border bg-background px-3 text-sm"
                    value={form.password}
                    onChange={(e) =>
                      setForm({ ...form, password: e.target.value })
                    }
                  />
                </Field>
              )}

              <div className="grid gap-4 sm:grid-cols-2">
                <Field label="Role">
                  <select
                    className="h-9 w-full rounded-lg border bg-background px-3 text-sm"
                    value={form.role}
                    onChange={(e) =>
                      setForm({
                        ...form,
                        role: e.target.value,
                        segment_ids: [],
                      })
                    }
                  >
                    {options.roles.map((role) => (
                      <option key={role}>{role}</option>
                    ))}
                  </select>
                </Field>

                <Field label="Kota">
                  <select
                    className="h-9 w-full rounded-lg border bg-background px-3 text-sm"
                    disabled={!isSuper}
                    value={form.city_id}
                    onChange={(e) =>
                      setForm({ ...form, city_id: Number(e.target.value) })
                    }
                  >
                    {options.cities.map((city) => (
                      <option key={city.id} value={city.id}>
                        {city.name}
                      </option>
                    ))}
                  </select>
                </Field>
              </div>

              {/* Coverage segment */}
              {form.role === "HD" && (
                <Field label="Coverage segment">
                  <div className="flex gap-3">
                    {options.segments.map((segment) => (
                      <label
                        className="flex items-center gap-2 text-sm"
                        key={segment.id}
                      >
                        <input
                          type="checkbox"
                          checked={form.segment_ids.includes(segment.id)}
                          onChange={() => toggleSegment(segment.id)}
                        />
                        {segment.code}
                      </label>
                    ))}
                  </div>
                </Field>
              )}

              {/* Coverage service (UI only, no DB binding) */}
              {form.role === "HD" && (
                <Field label="Coverage service">
                  <div className="flex gap-4">
                    <label className="flex items-center gap-2 text-sm cursor-pointer">
                      <input
                        type="checkbox"
                        checked={coverageServices.includes("provisioning")}
                        onChange={() => toggleCoverageService("provisioning")}
                      />
                      Provisioning
                    </label>
                    <label className="flex items-center gap-2 text-sm cursor-pointer">
                      <input
                        type="checkbox"
                        checked={coverageServices.includes("assurance")}
                        onChange={() => toggleCoverageService("assurance")}
                      />
                      Assurance
                    </label>
                  </div>
                </Field>
              )}

              <label className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={form.is_active}
                  onChange={(e) =>
                    setForm({ ...form, is_active: e.target.checked })
                  }
                />
                Akun aktif
              </label>

              <DialogFooter>
                <Button
                  type="button"
                  variant="outline"
                  onClick={() => setDialogOpen(false)}
                >
                  Batal
                </Button>
                <Button type="submit" disabled={saving}>
                  {saving ? "Menyimpan..." : "Simpan"}
                </Button>
              </DialogFooter>
            </form>
          </DialogContent>
        </Dialog>

        {/* Modal Reset Password */}
        <Dialog
          open={Boolean(resetUser)}
          onOpenChange={(open) => !open && setResetUser(null)}
        >
          <DialogContent>
            <DialogHeader>
              <DialogTitle>Reset Password</DialogTitle>
              <DialogDescription>
                Buat password baru untuk {resetUser?.name}.
              </DialogDescription>
            </DialogHeader>

            <form className="space-y-4" onSubmit={reset}>
              <input
                required
                minLength={8}
                type="password"
                placeholder="Minimal 8 karakter"
                className="h-9 w-full rounded-lg border bg-background px-3 text-sm"
                value={newPassword}
                onChange={(e) => setNewPassword(e.target.value)}
              />

              <DialogFooter>
                <Button
                  type="button"
                  variant="outline"
                  onClick={() => setResetUser(null)}
                >
                  Batal
                </Button>
                <Button type="submit" disabled={saving}>
                  Reset Password
                </Button>
              </DialogFooter>
            </form>
          </DialogContent>
        </Dialog>
      </div>
    </div>
  )
}

function Field({
  label,
  children,
}: {
  label: string
  children: React.ReactNode
}) {
  return (
    <label className="grid gap-1.5 text-sm font-medium">
      <span>{label}</span>
      {children}
    </label>
  )
}
