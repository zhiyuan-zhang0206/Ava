// Global next-intl type augmentation — makes useTranslations() key-checked
// against the messages/en catalog at compile time (a typo'd or removed
// key fails typecheck). The Messages type is anchored to messages/en, the
// canonical English source; messages/zh mirrors its shape (enforced at build
// time by the LanguageProvider import — both catalogs must stay structurally
// identical).
import type en from "../../messages/en";

declare module "next-intl" {
  interface AppConfig {
    Locale: "en" | "zh";
    Messages: typeof en;
  }
}

export {};
