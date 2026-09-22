# Behavior contract

[Message processing and reporting](docs/behavior.md) is the functional contract for this bot. Read the relevant flows and conditions before changing behavior.

- Compare implementation and tests against the contract, including authorization, routing order, moderation targets, and blacklist effects.
- When a change would deviate, establish whether the deviation is intended. Resolve consequential ambiguity with the requester rather than assuming the implementation is correct.
- For an intentional behavior change, update the contract and behavior tests in the same PR. For an implementation defect, restore the documented behavior; do not rewrite the contract merely to fit the code.
- Keep the contract in final-state language. Record change rationale in the PR, and update README usage instructions when affected.
