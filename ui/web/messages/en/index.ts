// The catalog is one JSON file per namespace; a new namespace is a new file plus its line here.
// `messages-layout.test.ts` pins that every file in this directory is listed, in both locales.

import agentAvailability from "./agentAvailability.json";
import agentRow from "./agentRow.json";
import alerts from "./alerts.json";
import common from "./common.json";
import config from "./config.json";
import contentToggle from "./contentToggle.json";
import contextBreakdown from "./contextBreakdown.json";
import contextMeter from "./contextMeter.json";
import control from "./control.json";
import displaySettings from "./displaySettings.json";
import fleet from "./fleet.json";
import guide from "./guide.json";
import insights from "./insights.json";
import inspector from "./inspector.json";
import inventory from "./inventory.json";
import login from "./login.json";
import markers from "./markers.json";
import memoryGraph from "./memoryGraph.json";
import metrics from "./metrics.json";
import nav from "./nav.json";
import noticeDetail from "./noticeDetail.json";
import openTasksNotice from "./openTasksNotice.json";
import pendingStrip from "./pendingStrip.json";
import presets from "./presets.json";
import runTimeline from "./runTimeline.json";
import schedules from "./schedules.json";
import sidebar from "./sidebar.json";
import skills from "./skills.json";
import spawn from "./spawn.json";
import timeline from "./timeline.json";

const messages = {
  agentAvailability,
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
  schedules,
  sidebar,
  skills,
  spawn,
  timeline,
};

export default messages;
