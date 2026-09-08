/* The weekly review.
 *
 * Prose and proposals, not figures. The week's numbers are the tables and the
 * chart elsewhere on this page, rendered from the same rows the review job was
 * shown — repeating them here would be a second copy that can disagree with
 * the first.
 *
 * The proposals are advisory and say so. Nothing in this system reads them
 * back and applies them: a risk limit changes when someone edits
 * `risklimits.py`, which is the point of the limits being deterministic and
 * outside the model's reach.
 */

import type { ProposedChange, Review } from "../api";
import { day, plain } from "../api";

/* Confidence as words as well as a number, because a bare 0.45 invites being
 * read as a probability of the change being correct. */
function confidence(value: number): string {
  if (value >= 0.75) return "high confidence";
  if (value >= 0.5) return "moderate confidence";
  return "low confidence";
}

function Proposal({ proposal, index }: { proposal: ProposedChange; index: number }) {
  return (
    <li className="proposal">
      <div className="proposal-head">
        <span className="proposal-index">{index}</span>
        <span className="proposal-change">{plain(proposal.change)}</span>
      </div>
      <div className="proposal-meta">
        <span className="pill">{proposal.area.replace(/_/g, " ")}</span>{" "}
        {confidence(proposal.confidence)} ({proposal.confidence.toFixed(2)})
      </div>
      <p>
        <strong>Why:</strong> {plain(proposal.rationale)}
      </p>
      <p>
        <strong>Expected effect:</strong> {plain(proposal.expected_effect)}
      </p>
    </li>
  );
}

export function WeeklyReview({ review }: { review: Review | null }) {
  if (review === null) {
    return (
      <div className="state">
        No weekly review yet — the first one is written on Sunday evening.
      </div>
    );
  }

  const proposals = review.recommendations ?? [];
  /* Blank-line separated, which is what the model was asked for. A single
   * paragraph is the common case and still renders correctly. */
  const paragraphs = plain(review.assessment ?? "")
    .split(/\n{2,}/)
    .map((p) => p.trim())
    .filter(Boolean);

  return (
    <>
      <div className="row-between">
        <div className="subtitle">
          Week of {day(review.period_start)} to {day(review.period_end)}
          {review.model ? ` · ${review.model}` : ""}
        </div>
        {/* A failed send is worth surfacing: the review is stored either way,
            so a silent failure would otherwise only be visible in the row. */}
        {review.email_status === "failed" && <span className="pill down">email failed</span>}
      </div>

      {paragraphs.length === 0 ? (
        <div className="state">This review has no written assessment.</div>
      ) : (
        paragraphs.map((paragraph, index) => <p key={index}>{paragraph}</p>)
      )}

      <h3>Proposed changes</h3>
      {proposals.length === 0 ? (
        <p className="hint">None: the week&rsquo;s figures did not support a change.</p>
      ) : (
        <>
          <p className="hint">
            Advisory only. Acting on one of these means editing the code — nothing here is
            applied automatically.
          </p>
          <ol className="proposals">
            {proposals.map((proposal, index) => (
              <Proposal key={index} proposal={proposal} index={index + 1} />
            ))}
          </ol>
        </>
      )}
    </>
  );
}
