"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  getConsultation,
  getConsultationSamples,
  getEpisodeStatus,
} from "@/lib/api";
import { deriveStage, STEPS, type EpisodeStage } from "@/lib/episode-stage";

function episodeIdOf(pathname: string): string | null {
  const match = /^\/episodes\/([^/]+)/.exec(pathname);
  return match ? (match[1] ?? null) : null;
}

/**
 * 全画面共通の上部ヘッダー: DaVinci Agent + 4段階の現在位置 +
 * 詳しい記録（同一画面の #details への素朴なアンカー）。
 *
 * 段階の根拠: /new-episode は素材固定、/references はなし、
 * /episodes/[id] は既存の status/consultation/samples 取得から
 * deriveStage で導く（取得失敗は「証拠なし」= ハイライトなし）。
 */
export default function SiteHeader() {
  const pathname = usePathname();
  const [stage, setStage] = useState<EpisodeStage | null>(
    pathname === "/new-episode" || pathname === "/" ? "素材" : null,
  );

  useEffect(() => {
    if (pathname === "/new-episode" || pathname === "/") {
      setStage("素材");
      return;
    }
    const id = episodeIdOf(pathname);
    if (id === null) {
      setStage(null);
      return;
    }
    let cancelled = false;
    void (async () => {
      const [status, consultations, samples] = await Promise.all([
        getEpisodeStatus(id).catch(() => null),
        getConsultation(id).catch(() => null),
        getConsultationSamples(id).catch(() => null),
      ]);
      if (cancelled) return;
      if (status === null && consultations === null && samples === null) {
        setStage(null);
        return;
      }
      setStage(deriveStage({ status, consultations, samples }));
    })();
    return () => {
      cancelled = true;
    };
  }, [pathname]);

  return (
    <header className="site-header" data-testid="site-header">
      <Link href="/" className="site-brand">
        DaVinci Agent
      </Link>
      <nav className="step-nav" aria-label="制作の段階">
        {STEPS.map((step, index) => (
          <span key={step} className="step-item">
            {index > 0 ? <span className="step-sep">→</span> : null}
            <span
              className="step"
              aria-current={stage === step ? "step" : undefined}
              data-testid={stage === step ? "step-current" : undefined}
            >
              {step}
            </span>
          </span>
        ))}
        <span className="step-mobile" data-testid="step-mobile">
          {stage !== null ? `${STEPS.indexOf(stage) + 1}/${STEPS.length} ${stage}` : ""}
        </span>
      </nav>
      <a href="#details" className="site-details-link">
        詳しい記録
      </a>
    </header>
  );
}
