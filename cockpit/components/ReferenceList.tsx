"use client";

import { useState } from "react";
import { classifyReference } from "@/lib/intake";

type ReferenceListProps = {
  items: string[];
  onAdd: (value: string) => void;
  onRemove: (index: number) => void;
};

const KIND_LABEL: Record<string, string> = {
  url: "URL",
  local: "ローカル",
  saved: "保存済み",
};

/**
 * Optional references (URL / local path / saved) — simple add/remove list.
 * Intake-only for now: the create API accepts just source + brief, so these
 * stay with the client until a later task carries them into the pipeline.
 */
export default function ReferenceList({
  items,
  onAdd,
  onRemove,
}: ReferenceListProps) {
  const [draft, setDraft] = useState("");

  const add = () => {
    const trimmed = draft.trim();
    if (trimmed === "") return;
    onAdd(trimmed);
    setDraft("");
  };

  return (
    <div>
      <div className="inline-row">
        <input
          type="text"
          id="reference-input"
          aria-label="参照（URL / ローカルパス / 保存済みID）"
          placeholder="https://… / /path/to/ref / 保存済み参照ID"
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.preventDefault();
              add();
            }
          }}
        />
        <button type="button" className="btn-small" onClick={add} disabled={draft.trim() === ""}>
          追加
        </button>
      </div>
      {items.length > 0 ? (
        <ul className="list-plain">
          {items.map((item, index) => (
            <li key={`${item}-${index}`}>
              <span>
                {KIND_LABEL[classifyReference(item)]}: {item}
              </span>
              <button
                type="button"
                className="btn-small"
                onClick={() => onRemove(index)}
                aria-label={`参照を削除: ${item}`}
              >
                削除
              </button>
            </li>
          ))}
        </ul>
      ) : (
        <p className="field-hint">任意です。後で追加することもできます。</p>
      )}
    </div>
  );
}
