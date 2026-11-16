import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api } from "../api/client";
import type { AccessOrganization, AccessPerson, Role } from "../api/admin";
import { CopyLine } from "../components/CopyLine";
import { usePermissions } from "../state/permissions";

/**
 * Who may use this knowledge base, managed by the people who use it.
 *
 * The server holds the rules (an Admin grants any role, a Contributor grants
 * Contributor or Reader in their own organization, nobody changes their own
 * access); this panel offers only what `grantable_roles` and `can_change` say
 * the caller may do, and shows the server's refusal when it gives one.
 */
export function AccessPanel() {
  const queryClient = useQueryClient();
  const { can } = usePermissions();
  const access = useQuery({ queryKey: ["access"], queryFn: api.access, retry: false });
  const refresh = () => queryClient.invalidateQueries({ queryKey: ["access"] });

  if (access.isLoading) return <p className="muted small">Loading…</p>;
  if (access.isError) {
    const error = access.error as Error;
    const off = error instanceof ApiError && error.status === 404;
    return (
      <div className={off ? "banner banner--info" : "banner banner--error"}>
        {off
          ? "Access to this region is set by its deployment, so there is nothing to manage here. " +
            "Turning on security.entra.delegated_access lets its users grant access themselves."
          : error.message}
      </div>
    );
  }
  const data = access.data!;
  const organizations = data.organizations;

  return (
    <>
      <section className="section">
        <h2 className="section__title">People</h2>
        <p className="muted small">
          You are {data.caller.role}.{" "}
          {data.grantable_roles.length
            ? `You can grant ${data.grantable_roles.join(", ")}.`
            : "You cannot grant access."}{" "}
          A person signs in with the name you enter here; until they do, the grant is pending.
        </p>
        <GrantForm
          roles={data.grantable_roles}
          organizations={organizations}
          homeTenant={data.home_tenant_id}
          isAdmin={data.caller.role === "Admin"}
          onDone={refresh}
        />
        <PeopleTable people={data.people} roles={data.grantable_roles} onChange={refresh} />
        {data.operator_admins.length ? (
          <p className="muted small" style={{ marginTop: 10 }}>
            Set by the operator's deployment and not changeable here:{" "}
            {data.operator_admins.length} administrator
            {data.operator_admins.length === 1 ? "" : "s"}.
          </p>
        ) : null}
      </section>

      {can("security.manage") ? (
        <section className="section">
          <h2 className="section__title">Organizations</h2>
          <p className="muted small">
            Connect a customer's Microsoft Entra directory once; its people then sign in with their
            organization's accounts and its own Admins and Contributors manage them. A directory
            that is not listed here cannot sign in.
          </p>
          <OrganizationForm onDone={refresh} />
          <OrganizationTable organizations={organizations} onChange={refresh} />
        </section>
      ) : null}
    </>
  );
}

function GrantForm({
  roles,
  organizations,
  homeTenant,
  isAdmin,
  onDone,
}: {
  roles: Role[];
  organizations: AccessOrganization[];
  homeTenant: string;
  isAdmin: boolean;
  onDone: () => void;
}) {
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<Role>(roles[0] ?? "Reader");
  const [organization, setOrganization] = useState("");
  const grant = useMutation({
    mutationFn: () =>
      api.grantAccess({ email: email.trim(), role, organization: organization || null }),
    onSuccess: () => {
      setEmail("");
      onDone();
    },
  });
  if (!roles.length) return null;
  return (
    <form
      className="admin-form"
      onSubmit={(event) => {
        event.preventDefault();
        if (email.trim()) grant.mutate();
      }}
    >
      <label className="field">
        <span>Sign-in name</span>
        <input
          className="text-input"
          type="email"
          placeholder="name@organization.com"
          value={email}
          onChange={(event) => setEmail(event.target.value)}
        />
      </label>
      <label className="field">
        <span>Role</span>
        <select
          className="text-input"
          value={role}
          onChange={(event) => setRole(event.target.value as Role)}
        >
          {roles.map((name) => (
            <option key={name} value={name}>
              {name}
            </option>
          ))}
        </select>
      </label>
      {isAdmin && organizations.length ? (
        <label className="field">
          <span>Organization</span>
          <select
            className="text-input"
            value={organization}
            onChange={(event) => setOrganization(event.target.value)}
          >
            <option value="">Mine</option>
            <option value={homeTenant}>Operator</option>
            {organizations.map((org) => (
              <option key={org.tenant_id} value={org.tenant_id}>
                {org.name}
              </option>
            ))}
          </select>
        </label>
      ) : null}
      <button className="btn btn--primary" disabled={!email.trim() || grant.isPending}>
        {grant.isPending ? "Granting…" : "Grant access"}
      </button>
      {grant.isError ? (
        <div className="banner banner--error admin-form__wide">{(grant.error as Error).message}</div>
      ) : null}
      {grant.isSuccess && grant.data.sign_in_url ? (
        <div className="admin-form__wide">
          <p className="muted small">Send them this link to sign in:</p>
          <CopyLine command={grant.data.sign_in_url} />
        </div>
      ) : null}
    </form>
  );
}

function PeopleTable({
  people,
  roles,
  onChange,
}: {
  people: AccessPerson[];
  roles: Role[];
  onChange: () => void;
}) {
  const [error, setError] = useState<string | null>(null);
  const settle = {
    onSuccess: () => {
      setError(null);
      onChange();
    },
    onError: (err: unknown) => setError((err as Error).message),
  };
  const change = useMutation({
    mutationFn: (input: { person: AccessPerson; role: Role }) =>
      api.grantAccess({
        email: input.person.email ?? "",
        role: input.role,
        organization: input.person.tenant_id,
      }),
    ...settle,
  });
  const remove = useMutation({ mutationFn: (id: string) => api.revokeAccess(id), ...settle });
  if (!people.length) return <p className="muted small">Nobody has been granted access yet.</p>;
  return (
    <>
      {error ? <div className="banner banner--error">{error}</div> : null}
      <div className="table-scroll">
        <table className="data-table">
          <thead>
            <tr>
              <th>Person</th>
              <th>Organization</th>
              <th>Role</th>
              <th>Granted by</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {people.map((person) => (
              <tr key={person.id}>
                <td>
                  {person.display_name ? `${person.display_name} · ` : ""}
                  {person.email ?? person.subject}
                  {person.you ? <span className="pill admin-pill">you</span> : null}
                  {person.pending ? <span className="pill pill--warn admin-pill">pending</span> : null}
                </td>
                <td>{person.organization}</td>
                <td>
                  {person.can_change && person.email ? (
                    <select
                      className="text-input text-input--small"
                      value={person.role}
                      aria-label={`Role of ${person.email}`}
                      disabled={change.isPending}
                      onChange={(event) =>
                        change.mutate({ person, role: event.target.value as Role })
                      }
                    >
                      {roles.map((name) => (
                        <option key={name} value={name}>
                          {name}
                        </option>
                      ))}
                    </select>
                  ) : (
                    person.role
                  )}
                </td>
                <td className="muted">{person.granted_by}</td>
                <td className="admin-actions">
                  {person.can_change ? (
                    <button
                      className="btn btn--small btn--danger"
                      disabled={remove.isPending}
                      onClick={() => {
                        if (window.confirm(`Remove ${person.email ?? "this person"}'s access?`))
                          remove.mutate(person.id);
                      }}
                    >
                      Remove
                    </button>
                  ) : null}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}

function OrganizationForm({ onDone }: { onDone: () => void }) {
  const [tenant, setTenant] = useState("");
  const [name, setName] = useState("");
  const [domains, setDomains] = useState("");
  const [role, setRole] = useState("");
  const connect = useMutation({
    mutationFn: () =>
      api.connectOrganization({
        tenant_id: tenant.trim(),
        name: name.trim() || undefined,
        domains: domains.split(/[\s,]+/).filter(Boolean),
        default_role: role || null,
      }),
    onSuccess: () => {
      setTenant("");
      setName("");
      setDomains("");
      onDone();
    },
  });
  return (
    <form
      className="admin-form"
      onSubmit={(event) => {
        event.preventDefault();
        if (tenant.trim()) connect.mutate();
      }}
    >
      <label className="field">
        <span>Entra tenant id</span>
        <input
          className="text-input"
          placeholder="00000000-0000-0000-0000-000000000000"
          spellCheck={false}
          value={tenant}
          onChange={(event) => setTenant(event.target.value)}
        />
      </label>
      <label className="field">
        <span>Name</span>
        <input className="text-input" value={name} onChange={(event) => setName(event.target.value)} />
      </label>
      <label className="field">
        <span>Domains</span>
        <input
          className="text-input"
          placeholder="contoso.com"
          value={domains}
          onChange={(event) => setDomains(event.target.value)}
        />
      </label>
      <label className="field">
        <span>Everyone in it gets</span>
        <select className="text-input" value={role} onChange={(event) => setRole(event.target.value)}>
          <option value="">Nothing (grant people one by one)</option>
          <option value="Reader">Reader</option>
          <option value="Contributor">Contributor</option>
        </select>
      </label>
      <button className="btn btn--primary" disabled={!tenant.trim() || connect.isPending}>
        {connect.isPending ? "Connecting…" : "Connect organization"}
      </button>
      {connect.isError ? (
        <div className="banner banner--error admin-form__wide">{(connect.error as Error).message}</div>
      ) : null}
    </form>
  );
}

function OrganizationTable({
  organizations,
  onChange,
}: {
  organizations: AccessOrganization[];
  onChange: () => void;
}) {
  const disconnect = useMutation({
    mutationFn: (tenant: string) => api.disconnectOrganization(tenant),
    onSuccess: onChange,
  });
  if (!organizations.length)
    return <p className="muted small">No customer directory is connected.</p>;
  return (
    <div className="admin-cards">
      {disconnect.isError ? (
        <div className="banner banner--error">{(disconnect.error as Error).message}</div>
      ) : null}
      {organizations.map((org) => (
        <div className="card admin-card" key={org.tenant_id}>
          <div className="admin-card__head">
            <strong>{org.name}</strong>
            <span className="pill">
              {org.default_role ? `everyone: ${org.default_role}` : "granted one by one"}
            </span>
            <button
              className="btn btn--small btn--danger"
              disabled={disconnect.isPending}
              onClick={() => {
                if (
                  window.confirm(
                    `Disconnect ${org.name}? Its people can no longer sign in and their grants are removed.`,
                  )
                )
                  disconnect.mutate(org.tenant_id);
              }}
            >
              Disconnect
            </button>
          </div>
          <div className="muted small">
            Tenant <code>{org.tenant_id}</code>
            {org.domains.length ? ` · ${org.domains.join(", ")}` : ""}
          </div>
          <p className="muted small">Their directory admin approves the app once, here:</p>
          <CopyLine command={org.admin_consent_url} />
          {org.sign_in_url ? (
            <>
              <p className="muted small">Their people sign in here:</p>
              <CopyLine command={org.sign_in_url} />
            </>
          ) : null}
        </div>
      ))}
    </div>
  );
}
