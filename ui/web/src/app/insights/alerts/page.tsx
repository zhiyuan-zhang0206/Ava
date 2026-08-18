// IM deep links point at /insights/alerts (the URL the alert IM messages
// carry). The alert history lives as a section of the Insights page
// (/insights#alerts) — this route forwards the link there.

import { redirect } from "next/navigation";

export default function AlertsRedirect() {
  redirect("/insights#alerts");
}
