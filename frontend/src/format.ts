export const fmtInt = (n: number) => n.toLocaleString('en-US')

/** "2026-09-28T10:42:04.910Z" -> "10:42:04" (UTC, like the logs). */
export function fmtTime(iso: string | null | undefined, withSeconds = true): string {
  if (!iso) return '—'
  const match = /T(\d{2}:\d{2})(:\d{2})?/.exec(iso)
  if (!match) return iso
  return withSeconds && match[2] ? match[1] + match[2] : match[1]
}

export function fmtDate(iso: string | null | undefined): string {
  return iso ? iso.slice(0, 10) : '—'
}

export function fmtBytes(n: number): string {
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / 1024 / 1024).toFixed(1)} MB`
}
