# Ownership

| Component | Owns |
| --- | --- |
| Clawake | Deployment desired state and lifecycle |
| OpenClaw | Runtime, conversations, A2A and subagents |
| ClawCAD | Engineering-domain behavior |
| Harness skill | Host bootstrap, deployment, diagnostics and runtime verification |

The harness is neither Robotics Lead nor Mechanical Engineer. It owns no
Engineering Contracts, CAD, Onshape, FeatureScript, Digital Twin decisions,
verification decisions, Human Gates, Ledger semantics, H2C, Bambu, manufacturing
or robot motion. Do not move product logic into Clawake. Exactly two permanent
ClawCAD gateways: Human/Discord → Robotics Lead → authenticated A2A → Mechanical
Engineer. Roles and allowlists remain product-owned. Do not implement fleet
management or branch-stack consolidation during bootstrap.
