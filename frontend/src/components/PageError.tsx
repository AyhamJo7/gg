export function PageError() {
  return <main className="main"><h1>This view could not be displayed</h1><p>GG has not been stopped. Reload the view or return to Overview to inspect current work.</p>
    <div className="row"><button onClick={() => window.location.reload()}>Reload view</button><a className="btn" href="#/" onClick={() => { window.location.hash = "#/"; window.location.reload(); }}>Open Overview</a></div></main>;
}
