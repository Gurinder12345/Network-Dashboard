interface PlaceholderPageProps {
  title: string;
}

export function PlaceholderPage({ title }: PlaceholderPageProps) {
  return (
    <>
      <h1 className="page-title">{title}</h1>
      <p className="page-subtitle">This section is not built yet.</p>
      <div className="placeholder-page">Coming in a later MVP step.</div>
    </>
  );
}
