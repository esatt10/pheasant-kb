#include "engine.hpp"
#include <vector>

namespace acme {

class Engine : public Base {
public:
    void run() override {
        auto items = loadItems();
        std::sort(items.begin(), items.end());
    }
};

int Engine::count() const {
    return helpers::countAll(this->items);
}

}
