import IntakeForm from "@/components/IntakeForm";

export default function NewEpisodePage() {
  return (
    <main className="page">
      <header className="page-header">
        <h1 className="page-title">新しい動画を作る</h1>
      </header>
      <IntakeForm />
    </main>
  );
}
