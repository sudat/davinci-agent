import IntakeForm from "@/components/IntakeForm";

export default function NewEpisodePage() {
  return (
    <main className="page">
      <header className="page-header">
        <h1 className="page-title">素材を選ぶ</h1>
        <p className="page-subtitle">
          撮影フォルダを選んで、作りたい動画をひとことで教えてください。
        </p>
      </header>
      <IntakeForm />
    </main>
  );
}
