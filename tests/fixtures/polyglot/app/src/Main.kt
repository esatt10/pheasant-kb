package com.acme

import com.acme.store.Store
import kotlinx.coroutines.launch

const val MAX_ITEMS = 10

data class Item(val id: String)

object Registry {
    fun register(item: Item): Boolean {
        return Store.save(item)
    }
}

fun main() = launch { Registry.register(Item("x")) }
