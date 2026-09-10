# Changelog

All notable changes to the Engine 1 Command Center are recorded here. Versions
follow [Semantic Versioning](https://semver.org/) (MAJOR.MINOR.PATCH):
MAJOR for breaking changes, MINOR for new backward-compatible features, PATCH
for fixes. The current version is shown in the app (bottom of the left nav,
and the sign-in screen) and via `GET /api/version`.

## [1.3.0] - 2026-09-09
### Added
- Combined Engine-1-wide Performance summary when "All PODS" is selected,
  instead of a "pick a POD" message — concatenates each POD's latest snapshot
  (AM rows tagged by POD, account rows included) so every total, pipeline-cover
  bar, and chart on the Performance tab works across all three PODS at once.

## [1.2.0] - 2026-09-09
### Added
- "All PODS" checkbox on the Performance tab's upload button (super_admin
  only) — imports the combined Engine-1-wide ACH workbook for all three PODS
  in one action directly from Performance, not just Settings.

## [1.1.2] - 2026-09-08
### Fixed
- Performance tab now actually requests the selected POD's data (`?pod=`) and
  re-renders immediately when the header POD selector changes, instead of
  always showing "no data" for podless roles regardless of which POD was picked.

## [1.1.1] - 2026-09-08
### Fixed
- The single-POD Performance import and AM Targets import now accept the
  combined Engine-1-wide ACH workbook too, matched to the importing POD by
  sheet name — previously they always read the first `PODS (N)` sheet
  regardless of which POD the upload was for.

## [1.1.0] - 2026-09-08
### Added
- **Multi-POD Engine1 ACH import** (super_admin only): one upload of the
  combined monthly workbook routes each `PODS (1)/(2)/(3)` sheet to its POD,
  updates per-AM targets and Performance snapshots, and rolls each POD's own
  Full-Year Target/Achieved/Recurring up to the sum of its AM figures.
- **PODS Head role** (`pod_head`): identical permissions to the existing
  pod-scoped `admin`, for whoever holds org-chart oversight of a POD.
- **super_admin can rename a username** (previously locked after creation).
- **Closed Lost** flag + reason on opportunities, independent of the existing
  Blocked flag.
- Ported this session's PODS-2 tracker upgrades: vertical Action Plan Timeline
  driven by independent Timeline Items, Team Task `source_team` + a searchable
  POD-scoped "Assigned to" picker, and the threaded/collapsible Weekly Meeting
  board with a Solved section, one-click Mark Solved/Needs Discussion,
  admin-tier delete, and a 7-day Get Weekly Summary.

## [1.0.0] - 2026-08-28
Baseline for version tracking - everything shipped before per-release
changelog entries started. Highlights, in order:
- Initial multi-POD Engine 1 Command Center (PODS 1/2/3, per-POD data
  isolation, row-level access control).
- `super_admin` role: full read/write across every POD.
- Sales Activity Tracker bulk import, plus a one-time PODS 1/2 data swap
  script (correcting which real team occupied which POD slot).
- Bulk-delete for opportunities on the Tracker.
