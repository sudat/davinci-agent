/**
 * Pure intake-form logic (no React, no DOM) — unit-tested by Vitest.
 * The Create button is enabled only when both the source folder and the
 * natural-language brief are non-empty after trimming.
 */

export type IntakeValues = {
  sourceFolder: string;
  briefText: string;
};

export type IntakeFieldErrors = {
  sourceFolder?: string;
  briefText?: string;
};

export function canCreateEpisode(values: IntakeValues): boolean {
  return values.sourceFolder.trim() !== "" && values.briefText.trim() !== "";
}

export function validateIntake(values: IntakeValues): IntakeFieldErrors {
  const errors: IntakeFieldErrors = {};
  if (values.sourceFolder.trim() === "") {
    errors.sourceFolder = "ソースフォルダを指定してください";
  }
  if (values.briefText.trim() === "") {
    errors.briefText = "動画の内容を入力してください";
  }
  return errors;
}

export type ReferenceKind = "url" | "local" | "saved";

/** Classify a reference string for the optional-references list UI. */
export function classifyReference(value: string): ReferenceKind {
  const trimmed = value.trim();
  if (/^https?:\/\//i.test(trimmed)) return "url";
  if (trimmed.startsWith("/") || trimmed.startsWith("~")) return "local";
  return "saved";
}
