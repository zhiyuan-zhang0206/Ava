// The catalog is one JSON file per namespace; a new namespace is a new file plus its line here.
// `messages-layout.test.ts` pins that every file in this directory is listed, in both locales.

import agentRow from "./agents/agentRow.json";
import alerts from "./operations/alerts.json";
import common from "./interface/common.json";
import config from "./operations/config.json";
import contentToggle from "./interface/contentToggle.json";
import contextBreakdown from "./agents/contextBreakdown.json";
import contextMeter from "./agents/contextMeter.json";
import control from "./operations/control.json";
import displaySettings from "./interface/displaySettings.json";
import fleet from "./operations/fleet.json";
import guide from "./interface/guide.json";
import insights from "./operations/insights.json";
import inspector from "./interface/inspector.json";
import inventory from "./operations/inventory.json";
import login from "./interface/login.json";
import markers from "./agents/markers.json";
import memoryGraph from "./operations/memoryGraph.json";
import metrics from "./operations/metrics.json";
import nav from "./interface/nav.json";
import noticeDetail from "./interface/noticeDetail.json";
import openTasksNotice from "./interface/openTasksNotice.json";
import pendingStrip from "./agents/pendingStrip.json";
import presets from "./agents/presets.json";
import runTimeline from "./agents/runTimeline.json";
import runTimelineSessions from "./agents/runTimelineSessions.json";
import schedules from "./operations/schedules.json";
import sidebar from "./agents/sidebar.json";
import skills from "./operations/skills.json";
import spawn from "./agents/spawn.json";
import timeline from "./agents/timeline.json";

const messages = {
  agentRow,
  alerts,
  common,
  config,
  contentToggle,
  contextBreakdown,
  contextMeter,
  control,
  displaySettings,
  fleet,
  guide,
  insights,
  inspector,
  inventory,
  login,
  markers,
  memoryGraph,
  metrics,
  nav,
  noticeDetail,
  openTasksNotice,
  pendingStrip,
  presets,
  runTimeline,
  runTimelineSessions,
  schedules,
  sidebar,
  skills,
  spawn,
  timeline,
};

export default messages;
