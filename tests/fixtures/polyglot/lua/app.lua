local store = require("acme.store")
-- function ghost() end

local function helper(x)
  return x
end

function App.render(id)
  local item = store.load(id)
  return helper(item)
end
