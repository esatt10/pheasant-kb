-module(acme_app).
-include("acme.hrl").
-import(acme_store, [load/1]).
-record(item, {id, name}).

% render(X) -> ghost.
render(Id) ->
    Item = acme_store:load(Id),
    format(Item).

format(Item) ->
    io_lib:format("~p", [Item]).
