import IntakeForm from "@/components/IntakeForm";

export default function NewEpisodePage() {
  return (
    <main className="page">
      <header className="page-header">
        <h1 className="page-title">新しいエピソード</h1>
        <p className="page-subtitle">
          ソースフォルダと概要だけあれば始められます。ほかは既定値で進みます。
        </p>
      </header>
      <IntakeForm />
    </main>
  );
}
