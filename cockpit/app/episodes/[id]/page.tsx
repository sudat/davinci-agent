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
        <h1 className="page-title">エピソード状況</h1>
        <p className="page-subtitle">ステージと進捗を表示しています。</p>
      </header>
      <OutputScopeProvider>
        <EpisodeView episodeId={id} />
        <KitPreviewPanel episodeId={id} />
        <ApprovalSessions episodeId={id} />
      </OutputScopeProvider>
      <ReferenceAnnotator episodeId={id} />
    </main>
  );
}
