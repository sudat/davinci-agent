"use client";

import { useState } from "react";
import {
  PREFERENCE_DOMAINS,
  type DomainPolarities,
  type Polarity,
  type PreferenceDomain,
} from "@/lib/api";
import { DOMAIN_LABEL, NAMED_POLARITIES, POLARITY_LABEL } from "@/lib/domains";

type DomainChipsProps = {
  domains: DomainPolarities;
  onChange: (next: DomainPolarities) => void;
};

function namedDomainList(domains: DomainPolarities): PreferenceDomain[] {
  return PREFERENCE_DOMAINS.filter((domain) => domains[domain] !== undefined);
}

/**
 * Editable domain/polarity preview (deterministic parse → operator
 * correction). Only NAMED domains carry a polarity — the hard domain-scoping
 * rule is enforced here visually: every chip names its domain and its
 * polarity select offers no "unspecified" option.
 */
export default function DomainChips({ domains, onChange }: DomainChipsProps) {
  const [draftDomain, setDraftDomain] = useState<PreferenceDomain | "">("");
  const named = namedDomainList(domains);
  const addable = PREFERENCE_DOMAINS.filter((domain) => domains[domain] === undefined);

  const setPolarity = (domain: PreferenceDomain, polarity: Polarity) => {
    onChange({ ...domains, [domain]: polarity });
  };

  const removeDomain = (domain: PreferenceDomain) => {
    const next = { ...domains };
    delete next[domain];
    onChange(next);
  };

  const addDomain = (domain: PreferenceDomain) => {
    onChange({ ...domains, [domain]: "like" });
  };

  return (
    <div data-testid="domain-chips">
      <p className="field-hint" data-testid="domain-scope-rule">
        ※ポラリティ（良い/嫌い/中立）を設定できるのは、名前を付けたドメインだけです。
      </p>
      {named.length > 0 ? (
        <div className="chip-row">
          {named.map((domain) => {
            const polarity = domains[domain] ?? "neutral";
            return (
              <span
                key={domain}
                className={`chip chip-${polarity}`}
                data-testid={`domain-chip-${domain}`}
              >
                {DOMAIN_LABEL[domain]}
                <select
                  value={polarity}
                  onChange={(event) =>
                    setPolarity(domain, event.target.value as Polarity)
                  }
                  aria-label={`${DOMAIN_LABEL[domain]}のポラリティ`}
                  data-testid={`polarity-select-${domain}`}
                >
                  {NAMED_POLARITIES.map((value) => (
                    <option key={value} value={value}>
                      {POLARITY_LABEL[value]}
                    </option>
                  ))}
                </select>
                <button
                  type="button"
                  className="btn-small"
                  onClick={() => removeDomain(domain)}
                  aria-label={`ドメインを削除: ${DOMAIN_LABEL[domain]}`}
                >
                  削除
                </button>
              </span>
            );
          })}
        </div>
      ) : (
        <p className="empty-note">
          ドメインが検出されませんでした。下から手動で追加できます。
        </p>
      )}
      {addable.length > 0 ? (
        <div className="inline-row" style={{ marginTop: "var(--space-2)" }}>
          <select
            value={draftDomain}
            onChange={(event) =>
              setDraftDomain(event.target.value as PreferenceDomain | "")
            }
            aria-label="追加するドメイン"
            data-testid="add-domain-select"
          >
            <option value="">ドメインを選択…</option>
            {addable.map((domain) => (
              <option key={domain} value={domain}>
                {DOMAIN_LABEL[domain]}
              </option>
            ))}
          </select>
          <button
            type="button"
            className="btn-small"
            onClick={() => {
              if (draftDomain === "") return;
              addDomain(draftDomain);
              setDraftDomain("");
            }}
            disabled={draftDomain === ""}
            data-testid="add-domain-button"
          >
            ドメインを追加
          </button>
          <span className="field-hint">追加したドメインには必ずポラリティが付きます。</span>
        </div>
      ) : null}
    </div>
  );
}
