"use client";

// Login form — accepts the cluster password and authenticates.

import { useTranslations } from "next-intl";
import { useState } from "react";

import { useAuth } from "@/lib/auth-context";
import { FLEX, FLEX_COL } from "@/lib/layout";
import { cn } from "@/lib/utils";

export function LoginForm() {
  const t = useTranslations("login");
  const { login } = useAuth();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const handleSubmit = async (e: React.SyntheticEvent<HTMLFormElement, SubmitEvent>) => {
    e.preventDefault();
    if (!password.trim()) return;
    setError("");
    setLoading(true);
    const result = await login(username.trim() || "admin", password.trim());
    if (result === "invalid-credentials") setError(t("invalidPassword"));
    else if (result === "rate-limited")
      setError(t("tooManyAttempts"));
    else if (result === "network-error") setError(t("connectionFailed"));
    setLoading(false);
  };

  return (
    <form
      onSubmit={handleSubmit}
      className={cn("w-full max-w-sm gap-4", FLEX, FLEX_COL)}
    >
      <div className={cn("gap-2", FLEX, FLEX_COL)}>
        <label htmlFor="username" className="text-sm font-medium">
          {t("username")}
        </label>
        <input
          id="username"
          type="text"
          name="username"
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          placeholder="admin"
          autoComplete="username"
          autoFocus
          disabled={loading}
          className="rounded-md border border-input bg-background px-3 py-2 text-sm ring-offset-background placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50"
        />
      </div>
      <div className={cn("gap-2", FLEX, FLEX_COL)}>
        <label htmlFor="password" className="text-sm font-medium">
          {t("password")}
        </label>
        <input
          id="password"
          type="password"
          name="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          placeholder={t("clusterSecret")}
          autoComplete="current-password"
          disabled={loading}
          className="rounded-md border border-input bg-background px-3 py-2 text-sm ring-offset-background placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50"
        />
      </div>
      {error && (
        <p className="text-sm text-destructive" role="alert">
          {error}
        </p>
      )}
      <button
        type="submit"
        disabled={loading || !password.trim()}
        className="inline-flex items-center justify-center rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground ring-offset-background transition-colors hover:bg-primary/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-50"
      >
        {loading ? t("signingIn") : t("signIn")}
      </button>
    </form>
  );
}
