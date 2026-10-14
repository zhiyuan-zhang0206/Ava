"use client";

import { useEffect, useId, useRef, useState } from "react";
import { useTranslations } from "next-intl";

import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import type { NoticeAnswer, NoticeQuestion } from "@/lib/contracts/types";
import { cn } from "@/lib/format/utils";
import { FLEX, MIN_W_0 } from "@/lib/layout/layout";

interface Draft {
  optionIds: string[];
  other: boolean;
  otherText: string;
  text: string;
  comment: string;
}

function answerFromDraft(question: NoticeQuestion, draft: Draft): NoticeAnswer | null {
  const comment = draft.comment.trim();
  if (question.type === "text") {
    return draft.text.trim() ? { text: draft.text.trim() } : null;
  }
  const count = draft.optionIds.length + Number(draft.other);
  if (!count || (question.type === "single_select" && count !== 1)) return null;
  if (draft.other && !draft.otherText.trim()) return null;
  return {
    option_ids: question.options.filter((o) => draft.optionIds.includes(o.id)).map((o) => o.id),
    ...(draft.other ? { other_text: draft.otherText.trim() } : {}),
    ...(comment ? { comment } : {}),
  };
}

/** Numbered choices share their frozen order with server replies and history. */
export function NoticeQuestionForm({
  question,
  pending,
  focusAnswer,
  onAnswerChange,
}: {
  question: NoticeQuestion;
  pending: boolean;
  focusAnswer: boolean;
  onAnswerChange: (answer: NoticeAnswer | null) => void;
}) {
  const t = useTranslations("noticeDetail");
  const fieldId = useId();
  const textRef = useRef<HTMLTextAreaElement>(null);
  const [draft, setDraft] = useState<Draft>({
    optionIds: [], other: false, otherText: "", text: "", comment: "",
  });
  const [noteOpen, setNoteOpen] = useState(false);

  useEffect(() => {
    onAnswerChange(answerFromDraft(question, draft));
  }, [question, draft, onAnswerChange]);

  useEffect(() => {
    if (focusAnswer) textRef.current?.focus();
  }, [focusAnswer]);

  if (question.type === "text") {
    return (
      <div className="space-y-2">
        <label htmlFor={fieldId} className="text-sm font-medium">{t("openQuestion")}</label>
        <Textarea
          id={fieldId}
          ref={textRef}
          value={draft.text}
          onChange={(e) => setDraft({ ...draft, text: e.target.value })}
          placeholder={t("openPlaceholder")}
          disabled={pending}
          className="min-h-28 text-base sm:text-sm"
        />
      </div>
    );
  }

  const single = question.type === "single_select";
  const choose = (id: string, checked: boolean) => {
    setDraft({
      ...draft,
      optionIds: single ? [id] : checked ? [...draft.optionIds, id] : draft.optionIds.filter((v) => v !== id),
      other: single ? false : draft.other,
    });
  };
  const optionClass = cn(FLEX, "min-h-12 items-start gap-3 rounded-lg border px-3 py-3 text-sm transition-colors has-[:focus-visible]:ring-2 has-[:focus-visible]:ring-ring");

  return (
    <div className="space-y-4">
      <fieldset disabled={pending} className="space-y-2 disabled:opacity-60">
        <legend className="mb-3 text-sm font-medium">{single ? t("singleSelect") : t("multiSelect")}</legend>
        {question.options.map((option, index) => {
          const selected = draft.optionIds.includes(option.id);
          return (
            <label key={option.id} className={cn(optionClass, selected ? "border-primary bg-primary/5" : "border-border hover:bg-muted/50")}>
              <input
                type={single ? "radio" : "checkbox"}
                name={fieldId}
                checked={selected}
                onChange={(e) => choose(option.id, e.target.checked)}
                className="mt-0.5 size-4 shrink-0 accent-primary"
              />
              <span className={cn("break-words", MIN_W_0)}>
                <span className="font-medium">{index + 1}. {option.label}</span>
                {option.description && <span className="mt-1 block text-sm text-muted-foreground">{option.description}</span>}
              </span>
            </label>
          );
        })}
        {question.allow_other && (
          <div className="space-y-2">
            <label className={cn(optionClass, draft.other ? "border-primary bg-primary/5" : "border-border hover:bg-muted/50")}>
              <input
                type={single ? "radio" : "checkbox"}
                name={fieldId}
                checked={draft.other}
                onChange={(e) => setDraft({ ...draft, other: e.target.checked, optionIds: single ? [] : draft.optionIds })}
                className="mt-0.5 size-4 shrink-0 accent-primary"
              />
              <span className="font-medium">{question.options.length + 1}. {t("other")}</span>
            </label>
            {draft.other && (
              <div className="space-y-2 pl-7">
                <label htmlFor={`${fieldId}-other`} className="text-sm">{t("otherAnswer")}</label>
                <Textarea
                  id={`${fieldId}-other`}
                  value={draft.otherText}
                  onChange={(e) => setDraft({ ...draft, otherText: e.target.value })}
                  placeholder={t("otherPlaceholder")}
                  className="min-h-24 text-base sm:text-sm"
                  required
                />
              </div>
            )}
          </div>
        )}
      </fieldset>
      <div className="space-y-2">
        <Button variant="ghost" size="sm" disabled={pending} onClick={() => setNoteOpen(!noteOpen)} aria-expanded={noteOpen} aria-controls={`${fieldId}-note-area`}>
          {t("additionalNote")}
        </Button>
        <div id={`${fieldId}-note-area`} hidden={!noteOpen} className="space-y-2">
          <label htmlFor={`${fieldId}-note`} className="sr-only">{t("additionalNote")}</label>
          <Textarea id={`${fieldId}-note`} value={draft.comment} onChange={(e) => setDraft({ ...draft, comment: e.target.value })} disabled={pending} placeholder={t("noteForAnswer")} className="text-base sm:text-sm" />
        </div>
      </div>
    </div>
  );
}

/** Read-only answer history keeps the complete numbering context for Other. */
export function NoticeQuestionSummary({ question, answer }: { question: NoticeQuestion; answer: NoticeAnswer | null | undefined }) {
  const t = useTranslations("noticeDetail");
  if (question.type === "text") {
    return (
      <div className="space-y-3 text-sm">
        {answer?.text && <p className="whitespace-pre-wrap break-words">{answer.text}</p>}
        {answer?.comment && <p className="whitespace-pre-wrap break-words">{t("additionalNote")}: {answer.comment}</p>}
      </div>
    );
  }
  return (
    <div className="space-y-3 text-sm">
      <ol className="space-y-2" aria-label={t("originalOptions")}>
        {question.options.map((option, index) => (
          <li key={option.id} className={cn("break-words", answer?.option_ids?.includes(option.id) ? "font-medium text-foreground" : "text-muted-foreground")}>
            {index + 1}. {option.label}
            {option.description && <span className="mt-1 block font-normal text-muted-foreground">{option.description}</span>}
            {answer?.option_ids?.includes(option.id) && <span className="ml-2 text-xs">{t("selected")}</span>}
          </li>
        ))}
        {question.allow_other && <li>{question.options.length + 1}. {t("other")}{answer?.other_text && <span className="ml-2 text-xs">{t("selected")}</span>}</li>}
      </ol>
      {answer?.other_text && <p className="whitespace-pre-wrap break-words">{t("other")}: {answer.other_text}</p>}
      {answer?.comment && <p className="whitespace-pre-wrap break-words"><span className="text-muted-foreground">{t("additionalNote")}: </span>{answer.comment}</p>}
    </div>
  );
}
