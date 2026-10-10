// The words for the kinds of events between agents and for the two ends of one.

import { useTranslations } from "next-intl";

import type { LinkKind } from "../model/timeline-links";

export function useLinkKindLabels(): Record<LinkKind, string> {
  const t = useTranslations("runTimeline");
  return {
    send_message: t("linkSendMessage"),
    spawn: t("linkSpawn"),
    fork: t("linkFork"),
    terminate: t("linkTerminate"),
    restart: t("linkRestart"),
    resurrect: t("linkResurrect"),
    notice: t("linkNotice"),
  };
}

/** An end of a link in words: `#405` for an agent, "User" for the user (no agent). */
export function useLinkEndLabel(): (agent: number | null) => string {
  const t = useTranslations("runTimeline");
  return (agent) => (agent === null ? t("userGroup") : `#${agent}`);
}
