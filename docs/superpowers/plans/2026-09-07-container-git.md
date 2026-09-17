# F5/F6 vertical slice: core Git inside the container boundary

1. Preserve the six failing worktree regressions and reproduce Git's absence in the real image.
2. Add Git to the non-root runtime image and expose truthful code-execution capabilities.
3. Support standalone contained repositories for core Git; refuse unsupported confined layouts
   and worktree operations before host staging. Preserve explicit local development behavior.
4. Prove reads leave bytes unchanged, selected commits change the expected content, and real
   repository hooks/textconv cannot cross the requested mount boundary. Verify Tool Manager
   events and A19's actual PARTIAL classification, plus linked-repository refusal.
5. Review, freeze production source, rebuild the image, run `just check`, then the full suite
   with coverage (base 91.53%). Commit this slice locally; publish only on explicit instruction.

The [DSH acceptance record](2026-09-07-dsh-acceptance.md) governs foundation completion.
This slice does not complete effective quotas, all execution surfaces, portable confined
worktrees or runtime-owned production backend selection. F5/F6 and A19 stay open.
