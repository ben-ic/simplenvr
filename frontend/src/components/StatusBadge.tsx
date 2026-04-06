import type { Camera } from "../types";

const config = {
  online: {
    label: "Connected",
    dotClass: "bg-green-500",
    bgClass: "bg-green-500/10 text-green-500",
  },
  needs_auth: {
    label: "Needs Login",
    dotClass: "bg-amber-400",
    bgClass: "bg-amber-400/10 text-amber-400 cursor-pointer hover:bg-amber-400/20",
  },
  offline: {
    label: "Offline",
    dotClass: "bg-red-500",
    bgClass: "bg-red-500/10 text-red-500",
  },
} as const;

export function StatusBadge({
  status,
  onClick,
}: {
  status: Camera["status"];
  onClick?: () => void;
}) {
  const c = config[status];
  return (
    <span
      className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded text-xs font-medium ${c.bgClass}`}
      onClick={status === "needs_auth" ? onClick : undefined}
    >
      <span className={`w-1.5 h-1.5 rounded-full ${c.dotClass}`} />
      {c.label}
    </span>
  );
}
