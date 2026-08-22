import EpisodeView from "@/components/EpisodeView";
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
      <EpisodeView episodeId={id} />
      <ReferenceAnnotator episodeId={id} />
    </main>
  );
}
