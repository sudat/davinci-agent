"use client";

import { useEffect, useState } from "react";

/**
 * Wall-clock `Date.now()` refreshed on a 1-second interval while `enabled`.
 * 表示用・停滞判定用の時計であり、データの正典にはならない（工程2P §A:
 * 状態取得時刻/経過時間の計測にのみ使用）。`enabled` が false の間は
 * interval を張らない（待機情報が非表示のときは動かさない）。
 */
export function useNow(enabled: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!enabled) return;
    setNow(Date.now());
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [enabled]);
  return now;
}
