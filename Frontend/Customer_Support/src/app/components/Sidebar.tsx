import { useEffect, useState } from "react";
import {
  LayoutDashboard, Users, Ticket, Phone, CheckSquare,
  Package, Star, BarChart3, Bell,
  UserCircle2, ChevronLeft, ChevronRight, LogOut, Archive,
} from "lucide-react";
import { useSupportIdentity } from "./SupportIdentityContext";
import { useTheme } from "./ThemeContext";

/* ─── Navigation config ─────────────────────────────── */
const NAV_GROUPS = [
  {
    label: null,
    items: [
      { id: "dashboard",    label: "Dashboard",    icon: LayoutDashboard },
    ],
  },
  {
    label: "Customers",
    items: [
      { id: "customers",    label: "Customers",    icon: Users       },
      { id: "archived",     label: "Archived",     icon: Archive     },
      { id: "tickets",      label: "Tickets",      icon: Ticket      },
      { id: "calls",        label: "Calls",        icon: Phone       },
      { id: "tasks",        label: "Tasks",        icon: CheckSquare },
    ],
  },
  {
    label: "Operations",
    items: [
      { id: "deliveries",   label: "Deliveries",   icon: Package     },
    ],
  },
  {
    label: "Insights",
    items: [
      { id: "satisfaction", label: "Satisfaction", icon: Star        },
      { id: "reports",      label: "Reports",      icon: BarChart3   },
    ],
  },
];

const BOTTOM_ITEMS = [
  { id: "notifications", label: "Notifications", icon: Bell     },
  { id: "profile",       label: "Profile",       icon: UserCircle2 },
];

/* ─── Sidebar colour palette (light & dark) ─────────── */
const SIDEBAR_LIGHT = {
  bg:           "linear-gradient(180deg, #1b152c 0%, #171225 50%, #0d0917 100%)",
  activeBg:     "linear-gradient(135deg, rgba(104,82,255,0.94), rgba(145,124,255,0.82))",
  activeText:   "#FFFFFF",
  hoverBg:      "rgba(255,255,255,0.08)",
  text:         "rgba(255,255,255,0.72)",
  textHover:    "#FFFFFF",
  groupLabel:   "rgba(255,255,255,0.36)",
  border:       "rgba(255,255,255,0.08)",
  userCardBg:   "rgba(255,255,255,0.05)",
  collapseBtn:  "rgba(255,255,255,0.05)",
  collapseBorder:"rgba(255,255,255,0.08)",
  collapseText: "rgba(255,255,255,0.60)",
  avatarBg:     "rgba(255,255,255,0.12)",
};

const SIDEBAR_DARK = {
  bg:           "linear-gradient(180deg, #09070f 0%, #100b19 56%, #171128 100%)",
  activeBg:     "linear-gradient(135deg, rgba(104,82,255,0.92), rgba(76,59,184,0.9))",
  activeText:   "#FFFFFF",
  hoverBg:      "rgba(255,255,255,0.06)",
  text:         "rgba(240,236,255,0.66)",
  textHover:    "#FFFFFF",
  groupLabel:   "rgba(255,255,255,0.28)",
  border:       "rgba(255,255,255,0.06)",
  userCardBg:   "rgba(255,255,255,0.04)",
  collapseBtn:  "rgba(255,255,255,0.04)",
  collapseBorder:"rgba(255,255,255,0.08)",
  collapseText: "rgba(240,236,255,0.56)",
  avatarBg:     "rgba(255,255,255,0.10)",
};

interface SidebarProps {
  currentPage: string;
  onNavigate:  (page: string) => void;
  collapsed:   boolean;
  onToggle:    () => void;
  onLogout:    () => void;
}

/* ─── Nav Item ───────────────────────────────────────── */
function NavItem({
  id, label, icon: Icon, isActive, collapsed, onClick, s, count, danger,
}: {
  id: string; label: string; icon: React.ElementType;
  isActive: boolean; collapsed: boolean; onClick: () => void; s: typeof SIDEBAR_LIGHT; count?: number; danger?: boolean;
}) {
  const [hovered, setHovered] = useState(false);

  return (
    <button
      onClick={onClick}
      title={collapsed ? label : undefined}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      className="relative w-full flex items-center rounded-xl transition-all"
      style={{
        gap: collapsed ? 0 : "10px",
        padding: collapsed ? "10px" : "10px 12px",
        justifyContent: collapsed ? "center" : "flex-start",
        background: isActive ? s.activeBg : hovered ? s.hoverBg : "transparent",
        borderRadius: "12px",
        boxShadow: isActive ? "0 16px 32px rgba(93, 71, 255, 0.22)" : "none",
      }}
    >
      <Icon
        size={18}
        strokeWidth={isActive ? 2.2 : 1.75}
        style={{ color: isActive ? s.activeText : hovered ? s.textHover : s.text, flexShrink: 0 }}
      />
      {!collapsed && (
        <span style={{
          fontSize: "0.875rem",
          fontWeight: isActive ? 600 : 400,
          color: isActive ? s.activeText : hovered ? s.textHover : s.text,
          flex: 1,
          textAlign: "left",
          letterSpacing: "-0.005em",
        }}>
          {label}
        </span>
      )}
      {!!count && <span className={`${collapsed ? "absolute ml-5 -mt-5" : ""} rounded-full px-1.5 py-0.5 text-[10px] font-extrabold ${danger ? "bg-red-600 text-white" : "bg-amber-400 text-gray-900"}`}>{count > 99 ? "99+" : count}</span>}
    </button>
  );
}

/* ─── Sidebar ────────────────────────────────────────── */
export function Sidebar({ currentPage, onNavigate, collapsed, onToggle, onLogout }: SidebarProps) {
  const { isDark } = useTheme();
  const { profile } = useSupportIdentity();
  const s = isDark ? SIDEBAR_DARK : SIDEBAR_LIGHT;
  const userName = profile?.name || "Loading user";
  const userRole = profile?.role || "Customer Support";
  const userInitials = profile?.avatar_initials || "U";
  const [pendingTasks, setPendingTasks] = useState(0);
  const [customerFollowups, setCustomerFollowups] = useState(0);
  const [missedCalls, setMissedCalls] = useState(0);

  useEffect(() => {
    const loadCount = () => fetch("/api/customer-support/tasks", { credentials: "same-origin" }).then(response => response.json()).then(data => { if (data.ok) setPendingTasks(data.counts?.Pending || 0); }).catch(() => undefined);
    const handleCount = (event: Event) => setPendingTasks((event as CustomEvent<number>).detail || 0);
    void loadCount();
    window.addEventListener("customer-support-task-count", handleCount);
    const timer = window.setInterval(loadCount, 30000);
    return () => { window.removeEventListener("customer-support-task-count", handleCount); window.clearInterval(timer); };
  }, []);
  useEffect(() => { const load = () => fetch("/api/customer-support/followups/count", { credentials: "same-origin" }).then(response => response.json()).then(data => { if (data.ok) setCustomerFollowups(data.count || 0); }).catch(() => undefined); const update = (event: Event) => { if (event instanceof CustomEvent && typeof event.detail === "number") setCustomerFollowups(event.detail); else void load(); }; void load(); window.addEventListener("customer-support-followup-count", update); window.addEventListener("customer-support-followups-changed", update); const timer = window.setInterval(load, 30000); return () => { window.removeEventListener("customer-support-followup-count", update); window.removeEventListener("customer-support-followups-changed", update); window.clearInterval(timer); }; }, []);
  useEffect(() => { const load = () => fetch("/api/customer-support/calls/missed-count", { credentials: "same-origin" }).then(response => response.json()).then(data => { if (data.ok) setMissedCalls(data.count || 0); }).catch(() => undefined); void load(); window.addEventListener("customer-support-missed-calls-changed", load); const timer = window.setInterval(load, 30000); return () => { window.removeEventListener("customer-support-missed-calls-changed", load); window.clearInterval(timer); }; }, []);

  return (
    <aside
      className="flex flex-col h-screen sticky top-0 flex-shrink-0 transition-all duration-200 overflow-x-hidden"
      style={{
        width: collapsed ? "68px" : "280px",
        background: s.bg,
        borderRight: `1px solid ${s.border}`,
      }}
    >
      {/* ── Logo ── */}
      <div
        className="flex items-center flex-shrink-0"
        style={{
          height: "72px",
          padding: collapsed ? "0 14px" : "0 20px",
          gap: "12px",
          borderBottom: `1px solid ${s.border}`,
        }}
      >
        <div
          className="flex-shrink-0 rounded-xl flex items-center justify-center overflow-hidden"
          style={{ width: collapsed ? "38px" : "156px", height: "42px", background: s.avatarBg, border: `1px solid rgba(255,255,255,0.3)` }}
        >
          <img
            src="https://imagedelivery.net/h9fmMoa1o2c2P55TcWJGOg/1c73f150-83b0-47b8-b173-3cdfdb5e4400/public"
            alt="Big Adom Enterprise"
            style={{ width: "100%", height: "100%", objectFit: "contain" }}
          />
        </div>

        {!collapsed && (
          <div className="min-w-0">
            <div style={{ fontSize: "0.6875rem", color: "rgba(255,255,255,0.48)", marginTop: "2px", letterSpacing: "0.08em", textTransform: "uppercase" }}>
              Customer Support Suite
            </div>
          </div>
        )}
      </div>

      {/* ── Navigation ── */}
      <nav className="flex-1 overflow-y-auto py-4" style={{ scrollbarWidth: "none" }}>
        {NAV_GROUPS.map((group, gi) => (
          <div key={gi} className="mb-1">
            {group.label && !collapsed && (
              <div style={{
                fontSize: "0.6875rem",
                fontWeight: 600,
                color: s.groupLabel,
                textTransform: "uppercase",
                letterSpacing: "0.08em",
                padding: "10px 20px 4px",
              }}>
                {group.label}
              </div>
            )}
            {group.label && collapsed && <div style={{ height: "8px" }} />}

            <div style={{ padding: "0 10px" }} className="space-y-0.5">
              {group.items.map(item => (
                <NavItem
                  key={item.id}
                  {...item}
                  isActive={currentPage === item.id}
                  collapsed={collapsed}
                  onClick={() => onNavigate(item.id)}
                  s={s}
                  count={item.id === "tasks" ? pendingTasks : item.id === "customers" ? customerFollowups : item.id === "calls" ? missedCalls : undefined}
                  danger={item.id === "calls"}
                />
              ))}
            </div>
          </div>
        ))}
      </nav>

      {/* ── Bottom ── */}
      <div className="flex-shrink-0 pb-4" style={{ borderTop: `1px solid ${s.border}` }}>
        <div style={{ padding: "12px 10px 0" }} className="space-y-0.5">
          {BOTTOM_ITEMS.map(item => (
            <NavItem
              key={item.id}
              {...item}
              isActive={currentPage === item.id}
              collapsed={collapsed}
              onClick={() => onNavigate(item.id)}
              s={s}
            />
          ))}

          <NavItem
            id="logout"
            label="Logout"
            icon={LogOut}
            isActive={false}
            collapsed={collapsed}
            onClick={onLogout}
            s={s}
          />
        </div>

        {/* Collapse toggle */}
        <div style={{ padding: "8px 10px 0" }}>
          <button
            onClick={onToggle}
            className="w-full flex items-center justify-center rounded-xl transition-all"
            style={{ height: "36px", gap: "6px", background: s.collapseBtn, color: s.collapseText, border: `1px solid ${s.collapseBorder}` }}
            onMouseEnter={e => { (e.currentTarget as HTMLElement).style.background = s.hoverBg; (e.currentTarget as HTMLElement).style.color = "#FFFFFF"; }}
            onMouseLeave={e => { (e.currentTarget as HTMLElement).style.background = s.collapseBtn; (e.currentTarget as HTMLElement).style.color = s.collapseText; }}
          >
            {collapsed
              ? <ChevronRight size={14} />
              : <><ChevronLeft size={14} /><span style={{ fontSize: "0.8125rem" }}>Collapse</span></>}
          </button>
        </div>

        {/* User profile */}
        {!collapsed && (
          <div
            className="mx-3 mt-3 flex items-center gap-2.5 rounded-xl px-3 py-2.5 cursor-pointer transition-all"
            style={{ background: s.userCardBg, border: `1px solid ${s.border}` }}
            onClick={() => onNavigate("profile")}
            onMouseEnter={e => (e.currentTarget as HTMLElement).style.background = s.hoverBg}
            onMouseLeave={e => (e.currentTarget as HTMLElement).style.background = s.userCardBg}
          >
            <div
              className="rounded-full flex items-center justify-center text-white flex-shrink-0"
              style={{ width: "30px", height: "30px", background: s.avatarBg, fontSize: "0.625rem", fontWeight: 700, border: "1px solid rgba(255,255,255,0.3)" }}
            >
              {userInitials}
            </div>
            <div className="flex-1 min-w-0">
              <div style={{ fontSize: "0.8125rem", fontWeight: 600, color: "#FFFFFF", lineHeight: 1.2, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                {userName}
              </div>
              <div style={{ fontSize: "0.6875rem", color: "rgba(255,255,255,0.52)", marginTop: "1px" }}>{userRole}</div>
            </div>
          </div>
        )}
      </div>
    </aside>
  );
}
