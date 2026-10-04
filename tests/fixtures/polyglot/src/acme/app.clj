(ns acme.app
  (:require [acme.store-api.core :as store]
            [clojure.string :as str]))

; (defn ghost [] nil)
(defrecord Item [id name])

(defn render [id]
  (let [item (store/fetch id)]
    (str/upper-case (:name item))))

(defn- helper [] (render 1))
