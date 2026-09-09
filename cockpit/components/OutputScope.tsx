"use client";

import { createContext, useContext, useState, type ReactNode } from "react";
import type { OutputGeometry, OutputId } from "@/lib/api";

export type OutputScopeState = {
  /** Registered outputs; null = unknown (not yet fetched, or an old
   *  backend without the route) → landscape-only UI, no output noise. */
  outputs: readonly OutputGeometry[] | null;
  selected: OutputId;
  setOutputs: (outputs: readonly OutputGeometry[] | null) => void;
  select: (output: OutputId) => void;
};

const OutputScopeContext = createContext<OutputScopeState | null>(null);

/** Shares the operator's selected output between EpisodeView (selector,
 *  preview, rebuild) and ApprovalSessions (per-output judgments) on the
 *  episode page. Without a provider the hook falls back to local state so
 *  each panel still renders standalone (landscape-only). */
export function OutputScopeProvider({ children }: { children: ReactNode }) {
  const [outputs, setOutputs] = useState<readonly OutputGeometry[] | null>(null);
  const [selected, select] = useState<OutputId>("landscape");
  return (
    <OutputScopeContext.Provider value={{ outputs, selected, setOutputs, select }}>
      {children}
    </OutputScopeContext.Provider>
  );
}

export function useOutputScope(): OutputScopeState {
  const shared = useContext(OutputScopeContext);
  const [localOutputs, setLocalOutputs] =
    useState<readonly OutputGeometry[] | null>(null);
  const [localSelected, selectLocal] = useState<OutputId>("landscape");
  if (shared !== null) return shared;
  return {
    outputs: localOutputs,
    selected: localSelected,
    setOutputs: setLocalOutputs,
    select: selectLocal,
  };
}

/** True when the episode registers more than the landscape default —
 *  the only state that shows the output selector. */
export function hasMultipleOutputs(
  outputs: readonly OutputGeometry[] | null,
): boolean {
  return outputs !== null && outputs.length > 1;
}
