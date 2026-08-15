"use client";

// A small modal that collects an initial prompt for a lifecycle action
// (fork / resurrect) and calls back with the trimmed, non-empty text.
// Submit is disabled while the textarea is blank — the "quick" no-prompt
// path is a separate menu item, so this dialog always carries a prompt.

import { useTranslations } from "next-intl";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Textarea } from "@/components/ui/textarea";

export interface PromptDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description?: string;
  placeholder?: string;
  submitLabel: string;
  /** Called with the trimmed prompt (never empty — submit is gated on it). */
  onSubmit: (prompt: string) => void;
}

export function PromptDialog({
  open,
  onOpenChange,
  title,
  description,
  placeholder,
  submitLabel,
  onSubmit,
}: PromptDialogProps) {
  const t = useTranslations("common");
  const [text, setText] = useState("");
  const trimmed = text.trim();

  const submit = () => {
    if (trimmed === "") return;
    onSubmit(trimmed);
    onOpenChange(false);
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) setText(""); // reset so the next open starts blank
        onOpenChange(next);
      }}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          {description ? (
            <DialogDescription>{description}</DialogDescription>
          ) : null}
        </DialogHeader>
        <Textarea
          autoFocus
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder={placeholder}
          // Cmd/Ctrl+Enter submits (matches the chat composer convention).
          onKeyDown={(e) => {
            if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
              e.preventDefault();
              submit();
            }
          }}
          className="min-h-28 font-mono text-xs"
        />
        <DialogFooter>
          <Button variant="ghost" size="sm" onClick={() => onOpenChange(false)}>
            {t("cancel")}
          </Button>
          <Button size="sm" onClick={submit} disabled={trimmed === ""}>
            {submitLabel}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
