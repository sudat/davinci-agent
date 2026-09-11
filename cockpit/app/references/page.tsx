import ReferencesView from "@/components/ReferencesView";

export default function ReferencesPage() {
  return (
    <main className="page">
      <header className="page-header">
        <h1 className="page-title">参考動画と好み</h1>
        <p className="page-subtitle">
          好きな動画を1件ずつ足して、好みとして保存します。2件たまると見比べられます。
        </p>
      </header>
      <ReferencesView />
    </main>
  );
}
