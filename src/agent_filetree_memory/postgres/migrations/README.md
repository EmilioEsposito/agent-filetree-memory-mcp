# Alembic integration

This directory is a package-owned Alembic version location. The host owns the
database connection, schema creation, and Alembic environment.

Append `package_version_location()` to the host's `version_locations`, call
`configure_host_alembic(config, schema="your_schema")`, and include
`migration_metadata("your_schema")` in the host's autogenerate metadata. The
initial revision forms an independent `agent_filetree_memory` branch. The
configuration helper preserves Alembic's implicit host
`<script_location>/versions` directory when it adds this package location.

Revision `afm_0001` contains frozen explicit DDL. It intentionally does not
import the evolving current SQLAlchemy metadata.

Migrations never read `DATABASE_URL` and never create a PostgreSQL schema.
