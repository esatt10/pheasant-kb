package com.acme.app;

import com.acme.store.Store;
import static com.acme.util.Strings.join;
import java.util.*;

/* class Ghost {} */
public class App extends Base {
    public static final int MAX_ITEMS = 10;

    public String render(String id) throws IOException {
        if (id == null) { return ""; }
        return join(Store.open(id).title(), "!");
    }

    private void close() {
        Store.shutdown();
    }
}
