# Invariants

## Invariants

| ID | Invariant | The Rule | Applies When | Owner | Derivation Rule | Tier | Mechanical Check | Failure Mode | Status | Superseded By |
|-|-|-|-|-|-|-|-|-|-|-|
| INV-001 | Golden authorization reloads identity | A signed token carries only user ID; token resolution reloads the current user and role before authorization. | Golden app signed-token authentication | AuthService | security dependencies call AuthService.verify_token; later role checks consume its resolved UserRecord and never decode token claims | 1 | auth property tests plus forged-role and deleted-user tests | stale or forged role grants access | active |  |
| INV-002 | Denied schedule operations never write | Missing, wrong-owner, and invalid schedule operations preserve persisted state. | Golden schedule create update or delete | ScheduleService | routers call the service policy owner rather than repositories directly | 1 | repository state assertions after every denied mutation | authorization denial still mutates a schedule | active |  |
