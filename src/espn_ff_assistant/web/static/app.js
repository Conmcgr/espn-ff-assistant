"use strict";

const content = document.querySelector("#content");
const refresh = document.querySelector("#refresh");
const pending = new Map();
const latest = new Map();
let current = "lineup";
let generation = 0;
let deadlineTimer;
const escapeHtml = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const e = escapeHtml;
const number = value => value == null ? "—" : Number(value).toFixed(1);
const signed = value => value == null ? "—" : `${value >= 0 ? "+" : ""}${number(value)}`;
const date = value => new Date(value).toLocaleString(undefined, {month:"short",day:"numeric",hour:"numeric",minute:"2-digit",timeZoneName:"short"});
const metric = (label, value, unit) => `<div class="metric"><p class="label">${label}</p><div class="value">${value} <small>${unit}</small></div></div>`;
const empty = (title, text) => `<div class="empty"><h3>${title}</h3><p>${e(text)}</p></div>`;
const heading = (title, subtitle) => `<div class="section-title"><div><h2>${title}</h2><p>${subtitle}</p></div></div>`;

function lineup(data) {
  const changed = data.rows.filter(r => r.changed).length;
  const explanation = data.gain > 0
    ? `${changed} slot change${changed === 1 ? "" : "s"} improve the projected lineup by ${number(data.gain)} points. Review the highlighted rows before changing anything in ESPN.`
    : data.summary;
  return heading("Your lineup", "The strongest eligible lineup from your roster, respecting players whose games have locked.") +
    `<div class="metrics">${metric("Current lineup", number(data.current_points), "projected pts")}${metric("Best legal lineup", number(data.optimal_points), "projected pts")}${metric("Potential gain", signed(data.gain), "projected pts")}</div>` +
    `<div class="explanation"><strong>${data.missing_projections ? "Incomplete projection coverage." : data.gain > 0 ? "A better lineup is available." : data.decision === "no_action" ? "Hold your lineup." : "Review your lineup."}</strong><p>${e(explanation)}</p></div>` +
    (data.missing_projections ? `<p class="notice">${e(data.missing_projections)} rostered player(s) have no projection and were valued at zero. Review the missing inputs before relying on this lineup.</p>` : "") +
    (data.unfilled_slots.length ? `<p class="notice">Unfilled eligible slots: ${data.unfilled_slots.map(e).join(", ")}. Check roster availability in ESPN.</p>` : "") +
    `<div class="table-wrap"><table><thead><tr><th>Best slot</th><th>Player</th><th class="num">Projection</th><th>Why / status</th></tr></thead><tbody>${data.rows.map(row => {
      const status = [row.locked ? "Game locked · held in current slot" : "Eligible for this slot", row.bye ? "Bye week" : "", row.injury && row.injury !== "ACTIVE" ? row.injury.replaceAll("_", " ") : "", row.projection == null ? "Projection missing · valued at 0" : "", row.changed ? `Move from ${row.current_slot}` : ""].filter(Boolean).join(" · ");
      return `<tr class="${row.changed ? "changed" : ""}"><td><span class="slot">${e(row.slot)}</span></td><td class="player">${e(row.name)}<span class="secondary">${e(row.position)}</span></td><td class="num">${number(row.projection)}${row.projection != null && row.effective_projection !== row.projection ? `<span class="secondary">${number(row.effective_projection)} risk-adjusted</span>` : ""}</td><td class="muted">${e(status)}</td></tr>`;
    }).join("")}</tbody></table></div><p class="caption">ESPN projections, not guaranteed scores. Questionable players are weighted at 85%; doubtful at 25%. Locked players stay put.</p>` +
    (data.close_calls.length ? `<details><summary>${data.close_calls.length} close lineup call(s)</summary><ul>${data.close_calls.map(call => `<li>${e(call.starter)} over ${e(call.alternative)} at ${e(call.slot)} by ${number(call.margin)} projected points. Small differences deserve a manual check.</li>`).join("")}</ul></details>` : "");
}

function waiverCard(pick, secondary = false) {
  const p = pick.payload, add = p.add, bid = p.bid;
  const available = add.availability === "waivers" ? "On waivers · claim required" : add.availability === "free_agent" ? "Free agent at snapshot" : "Check availability in ESPN";
  const action = {stash:"Stash",stream:"Stream",upgrade:"Add"}[p.kind] || "Add";
  return `<article class="card"><div class="card-top"><div><p class="label">${secondary ? "Below notification bar" : e(action)} · ${e(add.position)}</p><h3>${e(add.name)}</h3><p>${p.drop ? `Drop ${e(p.drop.name)}` : "Open roster spot"} · ${e(available)}</p></div><div class="bid">${bid ? `$${e(bid.amount)}` : "FA"}<small>${bid ? `FAAB · $${e(bid.low)}–$${e(bid.high)} range` : "No bid required"}</small></div></div>
    <div class="inline-stats"><div><strong>${signed(p.delta_next)}</strong><span>Week ${e(pick.scoring_period)}, pts</span></div><div><strong>${signed(p.delta_ros)}</strong><span>Rest of season, pts</span></div><div><strong>${number(add.percent_owned)}${add.percent_owned == null ? "" : "%"}</strong><span>ESPN ownership</span></div><div><strong>${e(pick.confidence || "Unrated")}</strong><span>Confidence</span></div></div>
    <details><summary>Why this pick?</summary><p>${e(pick.summary)}</p><ul><li>${e(add.name)}: ${number(add.week_projection)} projected points in Week ${e(pick.scoring_period)}; ${number(add.ros)} rest of season.</li>${p.drop ? `<li>${e(p.drop.name)}: ${number(p.drop.week_projection)} in Week ${e(pick.scoring_period)}; ${number(p.drop.ros)} rest of season.</li>` : ""}${bid ? `<li>Bid uses ${e(bid.comparables_used)} historical league claims (${e(bid.comparable_scope)}); ${e(bid.competitors.length)} rival team(s) with positional need. Confidence: ${e(bid.confidence)}.</li>` : ""}${secondary ? "<li>This option did not clear the recommendation gate or conflicts with a higher-ranked pick. It is not an additional action to take.</li>" : ""}</ul></details></article>`;
}

function waivers(data) {
  return heading("The waiver shortlist", "Add/drop pairs measured against your best existing lineup, with bids grounded in league history.") +
    (data.picks.length ? `<div class="cards">${data.picks.map(p => waiverCard(p)).join("")}</div>` : empty("No move worth forcing.", data.no_action || "No pickup clears the recommendation bar with the current data.")) +
    (data.other.length ? `<details class="other"><summary>${data.other.length} other evaluated options · not recommendations</summary><div class="cards">${data.other.map(p => waiverCard(p, true)).join("")}</div></details>` : "") +
    `<p class="caption">These are alternatives, not a sequence of claims. Check current availability, game locks, and your FAAB before making a move.</p>`;
}

function tradeCard(idea) {
  return `<article class="card" data-partner="${e(idea.partner)}"><div class="card-top"><div><p class="label">${e(idea.fit)} partner fit</p><h3>${e(idea.partner)}</h3></div></div><div class="trade-pair"><div><span class="label">You give</span><p>${idea.give.map(e).join(" + ")}</p></div><div><span class="label">You receive</span><p>${idea.receive.map(e).join(" + ")}</p></div></div>
    <div class="inline-stats"><div><strong class="good">${signed(idea.user_gain)}</strong><span>Your ROS points</span></div><div><strong class="${idea.partner_gain < 0 ? "bad" : "good"}">${signed(idea.partner_gain)}</strong><span>Their ROS points</span></div><div><strong>${signed(idea.net_gain)}</strong><span>Beyond best waiver alternative</span></div></div>
    ${idea.partner_gain < 0 ? `<p class="caption">The rival loses projected value. A gain for you does not make this a likely deal.</p>` : ""}
    <details><summary>Why this deal?</summary><ul><li>Legal under the model's ownership, roster, position and lock checks at the snapshot.</li><li>Your projected gain clears the model's uncertainty threshold and exceeds its best no-trade alternative.</li>${idea.why.map(s => `<li>${e(s)}</li>`).join("")}${idea.user_drops.length ? `<li>Your assumed drop(s): ${idea.user_drops.map(e).join(", ")}.</li>` : ""}${idea.partner_drops.length ? `<li>The rival must also drop: ${idea.partner_drops.map(e).join(", ")}.</li>` : ""}${idea.notes.map(s => `<li>${e(s)}</li>`).join("")}</ul><p>Partner fit is an ordinal heuristic, not an acceptance probability. Sparse history falls back to league-wide or neutral inputs.</p><div class="table-wrap"><table><thead><tr><th>Measured signal</th><th class="num">Value</th><th class="num">Sample</th><th>Basis / confidence</th></tr></thead><tbody>${idea.evidence.filter(s => s.name.startsWith("position_depth") || s.name.startsWith("trade_")).map(s => `<tr><td>${e(s.name.replaceAll("_", " "))}</td><td class="num">${number(s.value)}</td><td class="num">${e(s.sample_size)}</td><td>${e(s.basis)} / ${e(s.confidence)}<span class="secondary">${e(s.season_from)}–${e(s.season_to)}</span></td></tr>`).join("")}</tbody></table></div></details></article>`;
}

function trades(data) {
  if (data.deadline && Date.now() >= new Date(data.deadline).getTime()) {
    data = {...data, ideas: [], notice:"Your league's trade deadline has passed. No actionable trades are shown."};
  }
  return heading("Trade ideas, not trade offers", "One top idea per rival. Your projected gain and the rival's incentive are shown separately.") +
    (data.deadline ? `<p class="caption">League trade deadline: ${e(date(data.deadline))}.</p>` : "") +
    (data.missing_teams.length ? `<p class="notice">Incomplete roster data: ${data.missing_teams.map(e).join(", ")}. These teams were not evaluated.</p>` : "") +
    (data.ideas.length ? `<label class="filter">Rival <select id="partner-filter"><option value="">All rivals</option>${data.ideas.map(i => `<option>${e(i.partner)}</option>`).join("")}</select></label><div class="cards">${data.ideas.map(tradeCard).join("")}</div>` : empty("No trade to force right now.", data.notice)) +
    `<p class="caption">Rest-of-season values use ESPN projections. This is a capped search of up to two players per side, not an exhaustive market scan. Nothing is sent to a rival.</p>`;
}

function about() {
  return heading("A quiet assistant. A visible decision trail.", "The same recommendation engines as the agent, with the reasoning brought into view.") +
    `<div class="prose"><section><h3>1. Read the league</h3><p>The scheduled worker reads ESPN and saves private roster, injury, projection, waiver and transaction snapshots in Supabase. This dashboard reads those snapshots, not your ESPN credentials.</p></section><section><h3>2. Compare the alternatives</h3><p>The lineup engine finds the strongest legal assignment. Waiver picks compare an add/drop pair against that lineup. Trade ideas grade both teams and compare against the best no-trade alternative.</p></section><section><h3>3. Explain the call</h3><p>Open “Why this pick?” or “Why this deal?” for projections, measured rival signals, sample sizes and confidence. Scores and fit labels are estimates, never guarantees or calibrated acceptance odds.</p></section><section><h3>4. You make the move</h3><p>Check the latest status in ESPN, then act there if you agree. This frontend has no delivery, paid model calls or ESPN write actions. Texts are for later.</p></section><section><h3>Freshness and limits</h3><p>Week labels follow the latest completed live league snapshot. Calculations are cached for five minutes; Reload data checks the cache and refreshes older calculations in the background. A warning appears when the source snapshot is over eight hours old. Missing data is never presented as a confident recommendation.</p></section></div>`;
}

async function getSection(section) {
  if (pending.has(section)) return pending.get(section);
  const request = (async () => {
    for (;;) {
      const response = await fetch(`/api/${section}`, {cache:"no-store"});
      if (response.status === 202) { await new Promise(resolve => setTimeout(resolve, 2000)); continue; }
      if (!response.ok) throw new Error("League data is temporarily unavailable. Try Reload data in a minute.");
      const result = await response.json();
      latest.set(section, result);
      if (result.refreshing) {
        if (current === section) render(section, result);
        await new Promise(resolve => setTimeout(resolve, 2000)); continue;
      }
      return result;
    }
  })();
  pending.set(section, request);
  try { return await request; } finally { pending.delete(section); }
}

function render(section, result) {
  clearTimeout(deadlineTimer);
  if (section === "trades" && result.data.deadline) {
    const remaining = new Date(result.data.deadline).getTime() - Date.now();
    if (remaining > 0) deadlineTimer = setTimeout(() => {
      if (current === "trades") render(section, result);
    }, Math.min(remaining + 1, 2147483647));
  }
  document.querySelector("#league").textContent = `League A / ${result.season} / Week ${result.week}`;
  document.querySelector("#team").textContent = result.team;
  document.querySelector("#freshness").textContent = `ESPN snapshot ${date(result.synced_at)}${result.refreshing ? " · Recalculating…" : ""}`;
  const warning = document.querySelector("#warning");
  const stale = Date.now() - new Date(result.synced_at).getTime() > 8 * 3600 * 1000;
  warning.hidden = !stale && !result.refresh_failed;
  warning.textContent = result.refresh_failed ? "The latest calculation failed. Showing the previous result and its original snapshot time; verify everything in ESPN." : "The ESPN snapshot is over eight hours old. Treat these picks as historical until the worker saves a fresh sync.";
  content.innerHTML = ({lineup,waivers,trades})[section](result.data);
  content.setAttribute("aria-busy", "false");
  const filter = document.querySelector("#partner-filter");
  if (filter) filter.addEventListener("change", () => document.querySelectorAll("[data-partner]").forEach(card => { card.hidden = !!filter.value && card.dataset.partner !== filter.value; }));
}

async function show(section) {
  clearTimeout(deadlineTimer);
  if (!["lineup","waivers","trades","about"].includes(section)) section = "lineup";
  current = section;
  const token = ++generation;
  document.querySelectorAll("[data-section]").forEach(button => {
    if (button.dataset.section === section) button.setAttribute("aria-current", "page"); else button.removeAttribute("aria-current");
  });
  refresh.disabled = section === "about";
  if (section === "about") { content.innerHTML = about(); content.setAttribute("aria-busy", "false"); return; }
  if (latest.has(section)) {
    render(section, latest.get(section));
  } else {
    content.setAttribute("aria-busy", "true");
    content.innerHTML = `<div class="loading"><p class="label">Reading your league</p><h3>${section === "trades" ? "Checking trade ideas…" : "Calculating your options…"}</h3><p>Using saved ESPN projections and current roster rules. No moves are being made. You can explore the other tabs while this read-only calculation finishes.</p></div>`;
  }
  try { const result = await getSection(section); if (token === generation) render(section, result); }
  catch (error) {
    if (token !== generation) return;
    content.setAttribute("aria-busy", "false");
    content.innerHTML = `<div class="empty error"><h3>We couldn’t read your league.</h3><p>${e(error.message)}</p></div>`;
  }
}

document.querySelectorAll("[data-section]").forEach(button => button.addEventListener("click", () => { location.hash = button.dataset.section; }));
window.addEventListener("hashchange", () => show(location.hash.slice(1)));
refresh.addEventListener("click", () => show(current));
show(location.hash.slice(1) || "lineup");
// Warm the slower read-only calculations without holding up the first screen.
getSection("waivers").catch(() => {});
getSection("trades").catch(() => {});
