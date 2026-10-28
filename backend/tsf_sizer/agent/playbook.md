# Sizing playbook

This is the team's sizing policy. Apply it. You may deviate when the engineer's situation calls
for it, but say so and why, and record the deviation in the assumptions ledger.

## Method

- Requirement = measured usage x growth. Throughput, connections per second and sessions are
  also divided by the target utilization (default 70%), so the model runs below its rating.
- Default growth is 20% a year over 3 years, unless the engineer says otherwise.
- The throughput and connections per second in the Tech Support File are snapshots, not peaks.
  Ask for a known peak when it matters. Treat high CPU history as a warning that the snapshot
  may understate the load.
- Use the same requirements for every candidate so models can be compared.

## Fit modes

- `match_or_beat` (default): the replacement must match or beat every limit of the current
  model, and be strictly faster on performance. Use it for like for like replacements and
  upgrades.
- `usage_only`: the replacement only has to cover measured usage plus growth. Use it when the
  engineer wants a smaller or cheaper box, or when usage is far below the current model's limits
  (the box is oversized). A smaller model will be lower on some limits: that is expected, check
  each one against the usage.

## Which models

- Offer only models with data in the active capacity workbook, and only quotable ones.
- Previous-generation (superseded) families are left out unless the engineer asks for them.
- Models with static datasheet data (PA-800, PA-3200, PA-5200 series) are end of sale: they can
  be the current model, never a recommendation.
- Prefer the smallest model that fits with sensible headroom. Do not recommend a model with
  more than 5 times the current model's performance without saying it is far larger.
- Special variants (PoE, 5G, rugged): recommend only if the customer needs the feature. If the
  only models that fit are variants (for example every PA-1500 model is a PoE model), say so:
  the customer pays for hardware it does not use. This is a point for the engineer, not a
  reason to reject the model.

## Ports

- Default `port_rule` is `all`: the replacement offers at least the current model's port types.
- When the customer is downsizing, ask whether ports that are not in use are still needed. If
  not, use `used`.
- A 1G optic (SFP) can sit in a 10G cage (SFP+). Tell the engineer when the plan relies on it.
- Ports that show link up but have no configuration: ask whether they are cabled for a reason.
- If the customer no longer needs optics, or needs new ones, record it with `port_drop` or
  `port_add` and re-check.

## Known exceptions

- PA-800 series: the maximum number of aggregate interfaces never blocks a replacement, but
  report it whenever it applies.
- The Legacy Routing Engine is not relevant to the sizing. Do not mention it.
- A limit with no value in the workbook is unverified. Never invent it. List it in the open points
  for the engineer to check.
- The custom list (EDL) limit is the same on every model, so a larger model does not fix it.

## Investigate before you size

Look at the evidence for these and report what you find, even when it does not change the model:

- dataplane CPU peaks above 80%, and non-zero resource or allocation counters;
- expired licenses for features that are configured (for example GlobalProtect);
- decryption rules (the decryption share of the traffic is unknown from the file);
- Panorama management, HA mode, ports with link up and no configuration;
- limits where the current model is already above 60% used.

## Without a Tech Support File

- Some customers cannot provide a file. The engineer then enters what is known: the current
  model, load, interfaces in use, HA mode and a few counts.
- Ask for the figures that decide the model first: peak throughput, concurrent sessions,
  connections per second, then interfaces in use and HA mode. Ask for one thing at a time and
  accept "I don't know": a missing figure is listed as unverified, never guessed.
- Typical or estimated figures are weaker than peaks. Say which kind each figure is.
- Without a measured CPU history or resource counters, you cannot judge how loaded the current
  box really is. Say so, and add "confirm with a Tech Support File before quoting" to the open
  points.
- Use a larger growth margin when the figures are estimates, and say why.

## Customer documents

- Customers send PDF files and screenshots, for instance of the Panorama GUI with performance
  graphs. They are read inside the container: the PDF text, plus OCR of pictures.
- Look for peaks, not just the figure on screen: the maximum of the longest range shown beats an
  average or the last hour. Say which range a figure comes from.
- OCR misreads digits and units. Cross-check against other pages or figures, and have the
  engineer confirm any figure that decides the model. Never record a figure you doubt.
- If two sources disagree (for instance a Tech Support File snapshot and a screenshot peak),
  size on the higher one and say why.

## Verdict

- A recommendation names one model, or none if nothing fits, with the reason.
- Offer at most two alternatives, each with the situation where you would prefer it.
- Say why each close model was rejected, citing the limit that blocks it.
- Open points: everything the engineer must settle before quoting.
- Be honest about confidence. Low confidence is better than a wrong answer.
