// The words for the kinds of agent-to-agent events and for the two ends of one.

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
  };
}
