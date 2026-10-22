// A full pitch to real proportions on the data's 105 x 68 metre system, with all 22 starters and the ball.
// The SVG's units are metres; markers are placed by converting the same metres to percentages of the same box.
import league from "../../data/league.json";
import { LENGTH, WIDTH, layout, toScreen } from "./pitchLayout.js";

const VIEW = { x: -3, y: -1.5, w: LENGTH + 6, h: WIDTH + 3 }; // room outside the lines for the goals
const pct = (x, y) => ({ left: `${((x - VIEW.x) / VIEW.w) * 100}%`, top: `${((y - VIEW.y) / VIEW.h) * 100}%` });
const CLUBS = Object.fromEntries(league.clubs.map((club) => [club.id, club]));

const MID = WIDTH / 2;
const BOX = { depth: 16.5, width: 40.3 };
const SIX = { depth: 5.5, width: 18.3 };
const GOAL = { width: 7.32, depth: 2 };
const SPOT = 11;
const ARC = 9.15; // centre circle and penalty arc radius
const CORNER = 1;
const STRIPES = 12;

// The part of the 9.15 m circle around a penalty spot that lies outside the penalty area.
function dArc(side) {
  const spotX = side === "left" ? SPOT : LENGTH - SPOT;
  const edgeX = side === "left" ? BOX.depth : LENGTH - BOX.depth;
  const half = Math.sqrt(ARC ** 2 - (edgeX - spotX) ** 2);
  return `M ${edgeX} ${MID - half} A ${ARC} ${ARC} 0 0 ${side === "left" ? 1 : 0} ${edgeX} ${MID + half}`;
}

function Markings() {
  const end = (side) => {
    const goalLine = side === "left" ? 0 : LENGTH;
    const inward = side === "left" ? 1 : -1;
    const rect = (depth, width) => (
      <rect x={side === "left" ? 0 : LENGTH - depth} y={MID - width / 2} width={depth} height={width} />
    );
    return (
      <g key={side}>
        {rect(BOX.depth, BOX.width)}
        {rect(SIX.depth, SIX.width)}
        <circle className="spot" cx={goalLine + inward * SPOT} cy={MID} r={0.3} />
        <path d={dArc(side)} />
        <rect className="goal" x={side === "left" ? -GOAL.depth : LENGTH} y={MID - GOAL.width / 2}
              width={GOAL.depth} height={GOAL.width} />
      </g>
    );
  };
  const corners = [[0, 0, 1, 1], [LENGTH, 0, -1, 1], [0, WIDTH, 1, -1], [LENGTH, WIDTH, -1, -1]];
  return (
    <svg viewBox={`${VIEW.x} ${VIEW.y} ${VIEW.w} ${VIEW.h}`} aria-hidden="true">
      {Array.from({ length: STRIPES }, (_, i) => (
        <rect key={i} className={i % 2 ? "stripe dark" : "stripe"} x={(i * LENGTH) / STRIPES} y={0}
              width={LENGTH / STRIPES} height={WIDTH} />
      ))}
      <g className="lines">
        <rect x={0} y={0} width={LENGTH} height={WIDTH} />
        <line x1={LENGTH / 2} y1={0} x2={LENGTH / 2} y2={WIDTH} />
        <circle cx={LENGTH / 2} cy={MID} r={ARC} />
        <circle className="spot" cx={LENGTH / 2} cy={MID} r={0.3} />
        {end("left")}
        {end("right")}
        {corners.map(([x, y, dx, dy]) => (
          <path key={`${x}-${y}`}
                d={`M ${x + dx * CORNER} ${y} A ${CORNER} ${CORNER} 0 0 ${dx * dy > 0 ? 1 : 0} ${x} ${y + dy * CORNER}`} />
        ))}
      </g>
    </svg>
  );
}

export default function Pitch({ state }) {
  const ids = state?.score ? Object.keys(state.score) : league.clubs.map((club) => club.id);
  const clubs = ids.map((id) => CLUBS[id]).filter(Boolean);
  const ball = state?.ball;
  const players = layout(clubs, ids[0], ball);
  const ballXY = ball && toScreen(ball.team, ids[0], ball.x, ball.y);

  return (
    <div className="pitch-area">
      <div className="pitch">
        <Markings />
        {players.map((p) => (
          <span key={p.id} className={`player${p.onBall ? " on-ball" : ""}`} title={p.name}
                style={{ ...pct(p.x, p.y), "--primary": p.club.colours.primary, "--secondary": p.club.colours.secondary }}>
            {p.number}
          </span>
        ))}
        {ballXY && <span className="ball" style={pct(...ballXY)} />}
      </div>
    </div>
  );
}
