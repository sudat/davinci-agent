import ReferencesView from "@/components/ReferencesView";

export default function ReferencesPage() {
  return (
    <main className="page">
      <header className="page-header">
        <h1 className="page-title">参照</h1>
        <p className="page-subtitle">
          参照動画の登録と、保存した注釈の一覧を表示します。
        </p>
      </header>
      <ReferencesView />
    </main>
  );
}
