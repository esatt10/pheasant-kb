defmodule Acme.App do
  alias Acme.Store
  import Ecto.Query
  # def ghost, do: nil

  def render(id) do
    item = Store.fetch(id)
    format_item(item)
  end

  defp format_item(item), do: inspect(item)
end
