"use client";

// The icon vocabulary a plugin nav entry may name.
//
// A plugin declares an icon as DATA — a lucide name, never markup — and the
// console maps it onto a component it imports itself. That is the whole reason
// the vocabulary is a closed set: an unknown name has to be a manifest
// validation error rather than a blank nav row, so the two halves of the set
// (`shared/plugin_ui_contributions.py:NAV_ICONS` and this map) must stay
// identical. `plugin-nav-icon.test.ts` reads the Python tuple and asserts it.
//
// Adding an icon is a deliberate two-file change: the tuple and this map.

import {
  Activity,
  AppWindow,
  Bell,
  BookOpen,
  Bot,
  Calendar,
  ChartColumn,
  ChartLine,
  Clock,
  Coins,
  Cpu,
  Database,
  Eye,
  FileText,
  Folder,
  Gauge,
  GitBranch,
  Info,
  Kanban,
  Layers,
  LayoutDashboard,
  List,
  Lock,
  MessageSquare,
  Monitor,
  NotebookText,
  Package,
  Puzzle,
  Search,
  Server,
  Settings,
  Sparkles,
  Table,
  Terminal,
  TrendingUp,
  Users,
  Wallet,
  Waypoints,
  Workflow,
  Zap,
} from "lucide-react";

/** lucide name -> the component the console renders for it. */
export const PLUGIN_NAV_ICONS: Record<string, React.ComponentType<{ className?: string }>> = {
  "activity": Activity,
  "app-window": AppWindow,
  "bell": Bell,
  "book-open": BookOpen,
  "bot": Bot,
  "calendar": Calendar,
  "chart-column": ChartColumn,
  "chart-line": ChartLine,
  "clock": Clock,
  "coins": Coins,
  "cpu": Cpu,
  "database": Database,
  "eye": Eye,
  "file-text": FileText,
  "folder": Folder,
  "gauge": Gauge,
  "git-branch": GitBranch,
  "info": Info,
  "kanban": Kanban,
  "layers": Layers,
  "layout-dashboard": LayoutDashboard,
  "list": List,
  "lock": Lock,
  "message-square": MessageSquare,
  "monitor": Monitor,
  "notebook-text": NotebookText,
  "package": Package,
  "puzzle": Puzzle,
  "search": Search,
  "server": Server,
  "settings": Settings,
  "sparkles": Sparkles,
  "table": Table,
  "terminal": Terminal,
  "trending-up": TrendingUp,
  "users": Users,
  "wallet": Wallet,
  "waypoints": Waypoints,
  "workflow": Workflow,
  "zap": Zap,
};

/** The component for a declared icon name.
 *
 * A name outside the map cannot reach here through a validated manifest, so
 * the fallback is not a policy — it is what keeps one stale row from blanking
 * a nav surface if the two halves of the vocabulary ever drift. */
export function pluginNavIcon(name: string): React.ComponentType<{ className?: string }> {
  return PLUGIN_NAV_ICONS[name] ?? Puzzle;
}
