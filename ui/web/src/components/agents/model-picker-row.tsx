"use client";

import { useTranslations } from "next-intl";
import type { ModelsResponse } from "@/lib/contracts/types";
import { cn } from "@/lib/format/utils";

export const MODEL_PICKER_COLUMNS = "grid grid-cols-[minmax(0,1fr)_8.5rem_3rem] sm:grid-cols-[minmax(0,1fr)_10.5rem_3.5rem] items-center gap-2 sm:gap-3";

interface Props {
  model: string;
  info?: ModelsResponse["models"][string];
  selected: boolean;
  onSelect: () => void;
}

export function ModelPickerRow({ model, info, selected, onSelect }: Props) {
  const t = useTranslations("spawn");
  const price = info?.pricing;
  const tps = info?.reference_tps;
  return (
    <button
      type="button"
      onClick={onSelect}
      className={cn(
        MODEL_PICKER_COLUMNS,
        "w-full text-left px-3 py-1.5 text-sm hover:bg-sidebar-accent",
        selected && "bg-sidebar-accent/50",
      )}
    >
      <span className="truncate font-medium" title={model}>{model}</span>
      <span className="text-[11px] sm:text-xs text-right text-muted-foreground tabular-nums">
        {price
          ? `$${price.input.toFixed(2)}\u2009/\u2009$${price.cache_read.toFixed(2)}\u2009/\u2009$${price.output.toFixed(2)}`
          : "—"}
      </span>
      <span
        className="text-xs text-right text-muted-foreground tabular-nums"
        title={tps
          ? `${tps.note}\n${tps.source_url} · ${tps.source_checked_at}`
          : t("referenceTpsUnavailable")}
      >
        {tps?.display ?? "—"}
      </span>
    </button>
  );
}
