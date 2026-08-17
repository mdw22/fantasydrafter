import { useEffect, useMemo, useState } from "react";

const POSITIONS = ["QB", "RB", "WR", "TE"];
const TABS = ["ALL", ...POSITIONS];

// Persisted across reloads — a live draft can run long, and losing every
// checked-off pick to an accidental refresh would be a real problem, not
// just an inconvenience.
const DRAFTED_STORAGE_KEY = "fantasydrafter:draftedPlayerIds";

function loadDraftedIds() {
  try {
    const raw = localStorage.getItem(DRAFTED_STORAGE_KEY);
    return raw ? new Set(JSON.parse(raw)) : new Set();
  } catch {
    return new Set();
  }
}

function ValueBadge({ delta }) {
  // delta = vbd_vs_adp_proxy_overall: positive means the model likes this
  // player earlier than their prior-season finish (proxy) would suggest;
  // negative means the opposite.
  if (delta === null || delta === undefined || Number.isNaN(delta)) {
    return <span className="value-badge value-badge--neutral">—</span>;
  }
  if (delta > 0) {
    return <span className="value-badge value-badge--up">▲ {delta}</span>;
  }
  if (delta < 0) {
    return <span className="value-badge value-badge--down">▼ {Math.abs(delta)}</span>;
  }
  return <span className="value-badge value-badge--neutral">— 0</span>;
}

function PositionChip({ position }) {
  return <span className={`chip chip--${position}`}>{position}</span>;
}

function PlayerRow({ player, rankField, onDraft }) {
  return (
    <tr>
      <td className="col-draft">
        <input
          type="checkbox"
          className="draft-checkbox"
          aria-label={`Mark ${player.player_display_name} as drafted`}
          onChange={() => onDraft(player.player_id)}
        />
      </td>
      <td className="col-rank">{player[rankField]}</td>
      <td className="col-name">
        <PositionChip position={player.position} /> {player.player_display_name}
      </td>
      <td className="col-num">{player.projected_fantasy_points.toFixed(1)}</td>
      <td className="col-num">{player.vbd.toFixed(1)}</td>
      <td className="col-value">
        <ValueBadge delta={player.vbd_vs_adp_proxy_overall} />
      </td>
    </tr>
  );
}

function TierDivider({ tier }) {
  return (
    <tr className="tier-divider-row">
      <td colSpan={6}>
        <div className="tier-divider">
          <span className="tier-divider__chevron">▶</span>
          <span className="tier-divider__label">Tier {tier}</span>
          <span className="tier-divider__line" />
        </div>
      </td>
    </tr>
  );
}

export default function App() {
  const [players, setPlayers] = useState(null);
  const [error, setError] = useState(null);
  const [activeTab, setActiveTab] = useState("ALL");
  const [draftedIds, setDraftedIds] = useState(loadDraftedIds);

  useEffect(() => {
    // Fetched from public/data/cheat_sheet.json at runtime — regenerate
    // this file via scripts/csv_to_json.py before each build/deploy so
    // it reflects your latest cheat_sheet.csv output.
    fetch(`${import.meta.env.BASE_URL}data/cheat_sheet.json`)
      .then((res) => {
        if (!res.ok) throw new Error(`Failed to load data (${res.status})`);
        return res.json();
      })
      .then(setPlayers)
      .catch((err) => setError(err.message));
  }, []);

  useEffect(() => {
    localStorage.setItem(DRAFTED_STORAGE_KEY, JSON.stringify([...draftedIds]));
  }, [draftedIds]);

  const handleDraft = (playerId) => {
    setDraftedIds((prev) => new Set(prev).add(playerId));
  };

  const handleReset = () => {
    if (draftedIds.size === 0) return;
    if (!window.confirm(`Reset ${draftedIds.size} drafted player(s) back to the board?`)) {
      return;
    }
    setDraftedIds(new Set());
  };

  const rows = useMemo(() => {
    if (!players) return [];
    const available = players.filter((p) => !draftedIds.has(p.player_id));
    if (activeTab === "ALL") {
      return available.sort((a, b) => a.vbd_rank_overall - b.vbd_rank_overall);
    }
    return available
      .filter((p) => p.position === activeTab)
      .sort((a, b) => a.position_rank - b.position_rank);
  }, [players, activeTab, draftedIds]);

  // Group by tier only for a single-position view — tiers are computed
  // within each position, so a "Tier 1" QB and a "Tier 1" RB aren't
  // comparable. On the ALL tab we show a flat ranked list instead.
  const groupedByTier = useMemo(() => {
    if (activeTab === "ALL") return null;
    const groups = [];
    let currentTier = null;
    for (const player of rows) {
      if (player.tier !== currentTier) {
        currentTier = player.tier;
        groups.push({ tier: currentTier, players: [] });
      }
      groups[groups.length - 1].players.push(player);
    }
    return groups;
  }, [rows, activeTab]);

  return (
    <div className="app">
      <header className="scoreboard-header">
        <h1 className="scoreboard-header__title">Draft Board</h1>
        <p className="scoreboard-header__subtitle">
          14-team &middot; Full PPR &middot; Snake
        </p>
      </header>

      <nav className="tabs">
        {TABS.map((tab) => (
          <button
            key={tab}
            className={`tab ${activeTab === tab ? "tab--active" : ""}`}
            onClick={() => setActiveTab(tab)}
          >
            {tab}
          </button>
        ))}
        <div className="tabs__spacer" />
        <span className="drafted-count">{draftedIds.size} drafted</span>
        <button
          className="tab tab--reset"
          onClick={handleReset}
          disabled={draftedIds.size === 0}
        >
          Reset
        </button>
      </nav>

      <main className="board">
        {error && (
          <div className="empty-state">
            <p className="empty-state__title">Couldn't load the draft board.</p>
            <p className="empty-state__body">
              {error}. Run <code>scripts/csv_to_json.py</code> against your
              latest <code>cheat_sheet.csv</code> to generate{" "}
              <code>public/data/cheat_sheet.json</code>, then reload.
            </p>
          </div>
        )}

        {!error && !players && (
          <div className="empty-state">
            <p className="empty-state__title">Loading the board&hellip;</p>
          </div>
        )}

        {!error && players && (
          <table className="board-table">
            <thead>
              <tr>
                <th className="col-draft" aria-label="Drafted" />
                <th className="col-rank">Rk</th>
                <th className="col-name">Player</th>
                <th className="col-num">Proj</th>
                <th className="col-num">VBD</th>
                <th className="col-value">vs ADP</th>
              </tr>
            </thead>
            <tbody>
              {activeTab === "ALL"
                ? rows.map((player) => (
                    <PlayerRow
                      key={player.player_id}
                      player={player}
                      rankField="vbd_rank_overall"
                      onDraft={handleDraft}
                    />
                  ))
                : groupedByTier.map((group) => (
                    <FragmentGroup key={group.tier} group={group} onDraft={handleDraft} />
                  ))}
            </tbody>
          </table>
        )}
      </main>
    </div>
  );
}

function FragmentGroup({ group, onDraft }) {
  return (
    <>
      <TierDivider tier={group.tier} />
      {group.players.map((player) => (
        <PlayerRow
          key={player.player_id}
          player={player}
          rankField="position_rank"
          onDraft={onDraft}
        />
      ))}
    </>
  );
}
