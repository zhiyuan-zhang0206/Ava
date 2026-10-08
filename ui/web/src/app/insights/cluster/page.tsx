"use client";

import { useTranslations } from "next-intl";
import Link from "next/link";
import { useEffect, useState } from "react";

import { ClusterView } from "@/components/cluster-view/cluster-view";
import {
  parseClusterSelection,
  selectionQuery,
  toLocalInput,
  type ClusterSelection,
} from "@/components/cluster-view/cluster-selection";
import { buttonVariants } from "@/components/ui/button";
import { FLEX, FLEX_1, FLEX_COL, MIN_H_0, MIN_W_0 } from "@/lib/layout/layout";
import { cn } from "@/lib/format/utils";

/** The cluster view: one agent and everything it spawned or forked, over a time window. The
 *  selection lives in the URL (`?root=&from=&to=`) so a view can be linked. */
export default function ClusterPage() {
  const t = useTranslations("clusterView");
  const [selection, setSelection] = useState<ClusterSelection | null>(null);
  const [ready, setReady] = useState(false);
  const [root, setRoot] = useState("");
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");

  useEffect(() => {
    const parsed = parseClusterSelection(window.location.search, new Date());
    // eslint-disable-next-line react-hooks/set-state-in-effect -- reads the URL once after mount (SSR has no location)
    setSelection(parsed.selection);
    setRoot(parsed.selection === null ? "" : String(parsed.selection.root));
    setFrom(toLocalInput(parsed.form.from));
    setTo(toLocalInput(parsed.form.to));
    setReady(true);
  }, []);

  const valid =
    /^[0-9]+$/.test(root.trim()) && Number(root) >= 1 && from !== "" && to !== "" && Date.parse(from) < Date.parse(to);

  const apply = (event: React.SyntheticEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!valid) return;
    const next = { root: Number(root), from: new Date(from).toISOString(), to: new Date(to).toISOString() };
    window.history.replaceState(null, "", selectionQuery(next));
    setSelection(next);
  };

  return (
    <main id="main-content" className={cn(FLEX, FLEX_1, MIN_H_0, FLEX_COL)}>
      <header className={cn("items-center gap-3 border-b border-border px-4 py-2", FLEX)}>
        <Link href="/insights" className={buttonVariants({ size: "sm", variant: "ghost" })}>
          {t("backToInsights")}
        </Link>
        <h1 className={cn(FLEX_1, MIN_W_0, "truncate text-sm font-semibold")}>{t("title")}</h1>
      </header>
      <div className={cn("overflow-y-auto", FLEX_1, MIN_H_0)}>
        <div className="space-y-4 p-6">
          <form className={cn(FLEX, "flex-wrap items-end gap-3")} onSubmit={apply}>
            <label className="grid gap-1 text-xs text-muted-foreground">
              {t("rootAgent")}
              <input
                aria-label={t("rootAgent")}
                type="number"
                min="1"
                value={root}
                onChange={(event) => setRoot(event.target.value)}
                className="w-28 rounded border border-border bg-background px-2 py-1 font-mono text-xs text-foreground"
              />
            </label>
            <label className="grid gap-1 text-xs text-muted-foreground">
              {t("windowFrom")}
              <input
                aria-label={t("windowFrom")}
                type="datetime-local"
                value={from}
                onChange={(event) => setFrom(event.target.value)}
                className="rounded border border-border bg-background px-2 py-1 font-mono text-xs text-foreground"
              />
            </label>
            <label className="grid gap-1 text-xs text-muted-foreground">
              {t("windowTo")}
              <input
                aria-label={t("windowTo")}
                type="datetime-local"
                value={to}
                onChange={(event) => setTo(event.target.value)}
                className="rounded border border-border bg-background px-2 py-1 font-mono text-xs text-foreground"
              />
            </label>
            <button
              type="submit"
              disabled={!valid}
              className="rounded bg-primary px-2 py-1 text-xs text-primary-foreground hover:bg-primary/90 disabled:bg-muted disabled:text-muted-foreground"
            >
              {t("show")}
            </button>
          </form>
          {selection !== null ? (
            <ClusterView
              key={`${selection.root}-${selection.from}-${selection.to}`}
              root={selection.root}
              window={{ from: selection.from, to: selection.to }}
            />
          ) : ready ? (
            <p className="text-sm text-muted-foreground">{t("pickRoot")}</p>
          ) : null}
        </div>
      </div>
    </main>
  );
}
