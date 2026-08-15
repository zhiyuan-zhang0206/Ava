"use client";

// Login page — full-screen centered form.

import { useTranslations } from "next-intl";
import { useRouter } from "next/navigation";
import { useEffect } from "react";

import { LoginForm } from "@/components/auth/login-form";
import { useAuth } from "@/lib/auth-context";
import { FLEX } from "@/lib/layout";
import { cn } from "@/lib/utils";

export default function LoginPage() {
  const t = useTranslations("login");
  const { status } = useAuth();
  const router = useRouter();

  // Redirect to home if already authenticated
  useEffect(() => {
    if (status === "authenticated") {
      router.replace("/");
    }
  }, [status, router]);

  if (status === "loading") {
    return (
      <div className={cn("min-h-screen items-center justify-center", FLEX)}>
        <p className="text-muted-foreground">{t("checkingSession")}</p>
      </div>
    );
  }

  return (
    <div className={cn("min-h-screen items-center justify-center", FLEX)}>
      <div className="w-full max-w-sm space-y-6 rounded-lg border bg-card p-8 shadow-sm">
        <div className="space-y-2 text-center">
          <h1 className="text-2xl font-semibold tracking-tight">Ava</h1>
          <p className="text-sm text-muted-foreground">
            {t("enterPassword")}
          </p>
        </div>
        <LoginForm />
      </div>
    </div>
  );
}
