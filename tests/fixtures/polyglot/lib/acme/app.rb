require "json"
require_relative "store"
# def ghost; end

module Acme
  MAX_ITEMS = 10

  class App < Base
    def render(id)
      item = Store.find(id)
      format_item(item)
    end

    def self.build
      new
    end
  end
end
