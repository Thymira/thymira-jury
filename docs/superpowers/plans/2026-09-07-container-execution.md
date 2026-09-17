# ADR-0013 sandbox block: portable Python tools and container execution evidence

1. Reproduce the host-interpreter/path failure through the actual Python tool and the Docker
   launch-error classification in isolated tests before changing either implementation.
2. Reuse the existing locked `thymira:dev` image, clear its CLI entrypoint, disable implicit
   pulls, and separate container creation, execution, inspected child state and forced cleanup.
3. Execute the three Python tools with workspace-relative paths and the backend's interpreter;
   refuse read-only staging before it changes the workspace.
4. Remove the unsupported filesystem-quota claim. Confirmed container execution reports
   `partial`; unavailable execution or an unconfirmed completion reports `unusable`. Keep A19.
5. Replace synthetic container-tool coverage with real Python, experiment and inspection tools.
   Verify filesystem/network/resource boundaries, non-root execution and cleanup with Docker.
   Skip only absent prerequisites, never a backend failure after prerequisites are present.
6. Review the integrated diff, run the full gate and coverage (base 90.80%), build the image,
   and commit this slice locally. Publication requires the user's explicit instruction.

## Remaining sandbox work

Verify the remaining execution surfaces, resource and filesystem quotas, and permission metadata
before asserting full confinement or resolving A19. Then expose runtime-owned production
configuration and exercise the complete Run against that backend. Default API/worker factories
retain their safe local refusal during this slice.

Follow the agreed later order: record fidelity and credential exclusion, reasoning token/digest
evidence without content, then durable task/dependency/recovery coordination.
