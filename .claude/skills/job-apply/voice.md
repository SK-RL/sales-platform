# Voice rules for free-text answers

These apply to every cover letter, "why us", "why this role", "tell us
about a project", and any other open text box. The goal is simple: sound
like the person in `profile.yaml` writing quickly and honestly, not like
a template. Specific and plain beats polished and generic — and it is
also what actually gets read.

## Ground truth
- Every claim comes from `profile.yaml`. No invented employers, dates,
  metrics, tools, degrees, or team sizes. If the profile doesn't have it,
  the answer doesn't have it.
- Do not restate the job description back to them. They wrote it.
- One reason tied to something concrete about THIS company or role —
  a product, a stack choice from the JD, a scale problem. If nothing
  concrete is known, say something specific about the work instead.
  Never "I've always admired your mission".

## Shape
- Answer the question, then stop. "Why us": 60–120 words. Cover letter:
  120–180 words. Short questions: 1–3 sentences.
- Lead with a specific — a project, a number, a system — not with
  "I am writing to express my interest".
- Mix sentence lengths. Some short. Fragments are fine.
- First person, contractions are fine ("I've", "didn't").
- Plain paragraphs. No headers, bullets, or bold in a text box unless
  the field itself is a list.
- No sign-off lines ("I look forward to the opportunity…", "Thank you
  for your consideration"). End on the last real point.

## Words to never use
leverage, passionate, thrilled, excited to, synergy, cutting-edge,
state-of-the-art, dynamic, fast-paced, results-driven, seamless,
robust, spearheaded, utilize, align with, resonate, journey, delve,
tapestry, testament, "I am writing to", "as a highly motivated",
"proven track record", "in today's landscape".

## Patterns to avoid
- Em-dashes. Use a comma, a period, or parentheses.
- Triplets of adjectives ("reliable, scalable, and secure").
- Opening with the company name + "is a leader in".
- A paragraph that could be pasted into any other application unchanged.
- Perfectly even paragraph lengths.

## Example

Bad:
> I am writing to express my strong interest in the Senior DevOps
> Engineer position. As a highly motivated professional with a proven
> track record of leveraging cutting-edge cloud technologies, I am
> excited about the opportunity to contribute to your dynamic team and
> help drive seamless, scalable infrastructure.

Good (assuming these facts are in the profile):
> Last year I moved our payments stack from three hand-built EC2
> clusters onto EKS with Terraform and Argo. Deploys went from a
> Friday-afternoon ritual to about forty a week, and the on-call pager
> got quiet enough that people stopped dreading it. Your posting
> mentions you're mid-way through the same kind of move, which is the
> part of this work I like most: the awkward middle where half the
> traffic is on the new thing. Happy to talk through what went wrong
> for us the first time.

## Before typing an answer, check
1. Could this paragraph be sent to a different company unchanged? Rewrite.
2. Is every fact in profile.yaml? Remove anything that isn't.
3. Does it open with something concrete? If not, cut the first sentence.
4. Any banned words or em-dashes? Replace.
