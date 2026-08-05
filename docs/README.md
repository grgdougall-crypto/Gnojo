# Gnojo Documentation

This directory contains current implementation documentation, product standards, and forward-looking design material.

## Start here

- [Architecture](architecture.md): current application structure, storage, lifecycles, safety boundaries, and deployment status
- [Roadmap](roadmap.md): completed capabilities and the next planned phases
- [Brand](brand.md): Gnojo name, positioning, voice, and visual identity
- [Knowledge Base](knowledge-base.md): article lifecycle, sourcing, review, linking, and storage
- [Workflow Designer Specification](workflow-designer-spec.md): authoring interface behavior and constraints
- [Gnojo Curator](curator.md): read-only content auditing, reports, filters, and trust boundaries

## Product standards

- [Product Architecture](product-architecture.md)
- [Design System](design-system.md)
- [Taxonomy](TAXONOMY.md)
- [Command Library](command-library.md)
- [Decision Trees](decision-trees.md)
- [Vision](vision.md)

## Forward-looking references

- [Gnojo 2.0 Blueprint](gnojo-2.0-blueprint.md)
- [UI Wireframes](ui-wireframes.md)

Forward-looking documents describe direction and design intent. They should not be read as proof that every capability is implemented. The README, architecture, roadmap, automated tests, and running application are the authoritative sources for current repository status.

GitHub renders the Mermaid diagrams in the README, architecture, and knowledge-base documentation. Keep diagrams small, label decisions clearly, and update the surrounding prose whenever a flow changes.

## Documentation maintenance

When a feature changes:

1. Update the README if the public capability or setup changes.
2. Update architecture when storage, boundaries, or component responsibilities change.
3. Update the roadmap when a milestone is completed or reprioritized.
4. Update the relevant content standard or specification.
5. Run tests and check documentation links before committing.
