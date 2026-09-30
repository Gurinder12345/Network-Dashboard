interface ConfigViewProps {
  parents: string[] | null;
  lines: string[] | null;
}

/** Read-only change body: parent/context strip, then numbered command lines. */
export function ConfigView({ parents, lines }: ConfigViewProps) {
  const context = parents ?? [];
  const commands = lines ?? [];

  return (
    <div className="config-view">
      <div className="config-view-context">
        <span className="form-label">Context</span>
        {context.length > 0 ? (
          context.map((parent, index) => (
            <code key={index}>
              {index > 0 ? "› " : ""}
              {parent}
            </code>
          ))
        ) : (
          <span className="muted">global configuration</span>
        )}
      </div>
      <pre className="config-view-lines">
        {commands.length === 0
          ? "(no commands)"
          : commands.map((line, index) => (
              <div key={index}>
                <span className="line-no">{index + 1}</span>
                {line}
              </div>
            ))}
      </pre>
    </div>
  );
}
