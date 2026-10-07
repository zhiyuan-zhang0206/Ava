"use client";

import { useTranslations } from "next-intl";
import type { ModelsResponse } from "@/lib/contracts/types";
import { cn } from "@/lib/format/utils";
import { formatModelPrice } from "@/lib/format/format-price";

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
  const priceTitle = price ? [
    t("priceBreakdown"),
    t("pricingConditions"),
    ...(price.cache_write_5m != null ? [t("cacheWritePrice", {
      duration: "5m", price: formatModelPrice(price.cache_write_5m),
    })] : []),
    ...(price.cache_write_1h != null ? [t("cacheWritePrice", {
      duration: "1h", price: formatModelPrice(price.cache_write_1h),
    })] : []),
  ].join("\n") : undefined;
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
      <span className="text-[11px] sm:text-xs text-right text-muted-foreground tabular-nums" title={priceTitle}>
        {price
          ? `${formatModelPrice(price.input)}\u2009/\u2009${formatModelPrice(price.cache_read)}\u2009/\u2009${formatModelPrice(price.output)}`
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
