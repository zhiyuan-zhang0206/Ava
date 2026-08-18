// Global next-intl type augmentation — makes useTranslations() key-checked
// against the en.json message catalog at compile time (a typo'd or removed
// key fails typecheck). The Messages type is anchored to en.json, the
// canonical English source; zh.json mirrors its shape (enforced at build
// time by the LanguageProvider import — both files must stay structurally
// identical).
import type en from "../../messages/en.json";

declare module "next-intl" {
  interface AppConfig {
    Locale: "en" | "zh";
    Messages: typeof en;
  }
}

export {};
