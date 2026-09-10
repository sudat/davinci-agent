import ApprovalSessions from "@/components/ApprovalSessions";
import EpisodeView from "@/components/EpisodeView";
import KitPreviewPanel from "@/components/KitPreviewPanel";
import { OutputScopeProvider } from "@/components/OutputScope";
import ReferenceAnnotator from "@/components/ReferenceAnnotator";

export default async function EpisodeStatusPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  return (
    <main className="page">
      <header className="page-header">
        <h1 className="page-title">動画をつくる</h1>
        <p className="page-subtitle">今の段階に合わせて、必要な操作だけを表示しています。</p>
      </header>
      <OutputScopeProvider>
        <EpisodeView episodeId={id} />
        <details className="episode-details">
          <summary>その他の記録</summary>
          <KitPreviewPanel episodeId={id} />
          <ApprovalSessions episodeId={id} />
          <ReferenceAnnotator episodeId={id} />
        </details>
      </OutputScopeProvider>
    </main>
  );
}
